"""Safe, provider-independent recovery for failed website knowledge sources."""

from __future__ import annotations

import asyncio
import html
import io
import ipaddress
import json
import socket
from collections import Counter
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx
import pymupdf
from bs4 import BeautifulSoup, Tag

from app.services.integration_security import IntegrationConfigError, validate_public_https_url
from app.services.knowledge_records import (
    RECORD_SEPARATOR,
    KnowledgeRecord,
    dedupe_records,
    make_record,
    render_records,
)

MAX_WEBSITE_BYTES = 5 * 1024 * 1024
MAX_BROWSER_API_BYTES = 2 * 1024 * 1024
MAX_BROWSER_API_BODY_BYTES = 256 * 1024
MAX_BROWSER_API_REQUESTS = 24
MAX_EXTRACTED_CHARS = 500_000
MAX_PROVIDER_PDF_BYTES = 8 * 1024 * 1024
MIN_USEFUL_CHARS = 120
STATIC_RENDER_THRESHOLD = 2_000
MAX_REDIRECTS = 5
FETCH_ATTEMPTS = 3
FETCH_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
_CONTENT_TAGS = ("h1", "h2", "h3", "h4", "p", "li", "dt", "dd", "tr", "blockquote")


class WebsiteRecoveryError(RuntimeError):
    """Bounded, user-safe failure raised by the website recovery pipeline."""

    def __init__(self, message: str, *, code: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class RecoveredPage:
    url: str
    title: str
    text: str
    method: str
    downloaded_bytes: int
    # Directory pages often paginate ("Next"); the count of pages whose
    # records were merged into ``text``.
    pages: int = 1


def recovery_metadata(
    metadata: dict | None,
    *,
    stage: str,
    status: str = "processing",
    message: str | None = None,
    method: str | None = None,
    extracted_characters: int | None = None,
) -> dict:
    """Return metadata with one durable, UI-readable recovery progress record."""
    value = dict(metadata or {})
    current = dict(value.get("recovery") or {})
    current.update(
        {
            "stage": stage,
            "status": status,
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )
    if message is not None:
        current["message"] = message[:500]
    if method is not None:
        current["method"] = method
    if extracted_characters is not None:
        current["extracted_characters"] = extracted_characters
    value["recovery"] = current
    return value


def _canonical_hostname(hostname: str) -> str:
    return hostname.rstrip(".").encode("idna").decode("ascii").lower()


def _is_related_site_hostname(page_hostname: str, request_hostname: str | None) -> bool:
    """Allow the site's own host and its subdomains, including APIs behind www sites."""
    if not request_hostname:
        return False
    trusted_site = _canonical_hostname(page_hostname).removeprefix("www.")
    candidate = _canonical_hostname(request_hostname)
    return candidate == trusted_site or candidate.endswith(f".{trusted_site}")


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect to the public IP validated immediately before the request."""

    def __init__(self, hostname: str, address: str):
        self._hostname = _canonical_hostname(hostname)
        self._address = address
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        if _canonical_hostname(host) != self._hostname:
            raise OSError("website_destination_changed")
        return await self._backend.connect_tcp(
            self._address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, *args, **kwargs):
        raise OSError("website_unix_socket_forbidden")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, hostname: str, address: str):
        super().__init__(
            trust_env=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        )
        self._pool._network_backend = _PinnedNetworkBackend(hostname, address)


async def _resolve_public_destination(url: str) -> tuple[str, str]:
    try:
        validate_public_https_url(url)
    except IntegrationConfigError as exc:
        raise WebsiteRecoveryError(
            str(exc).replace("Integration URL", "Website URL"),
            code="unsafe_url",
        ) from exc
    hostname = urlsplit(url).hostname
    if not hostname:
        raise WebsiteRecoveryError("Website URL has no hostname.", code="invalid_url")
    try:
        records = await asyncio.wait_for(
            asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                443,
                type=socket.SOCK_STREAM,
            ),
            timeout=4.0,
        )
    except (TimeoutError, OSError, socket.gaierror) as exc:
        raise WebsiteRecoveryError(
            "The website hostname could not be resolved.",
            code="dns_failed",
            retryable=True,
        ) from exc
    addresses = tuple(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise WebsiteRecoveryError(
            "The website hostname returned no network address.",
            code="dns_failed",
            retryable=True,
        )
    try:
        parsed_addresses = [ipaddress.ip_address(address) for address in addresses]
    except ValueError as exc:
        raise WebsiteRecoveryError("The website address is invalid.", code="unsafe_url") from exc
    if any(not address.is_global for address in parsed_addresses):
        raise WebsiteRecoveryError(
            "Website URL must resolve only to public internet addresses.",
            code="unsafe_url",
        )
    return hostname, addresses[0]


async def _download_once(url: str) -> tuple[int, dict[str, str], bytes]:
    hostname, address = await _resolve_public_destination(url)
    transport = _PinnedAsyncHTTPTransport(hostname, address)
    headers = {
        "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.6",
        "User-Agent": "VAV-Knowledge-Recovery/1.0 (+website knowledge indexing)",
    }
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=FETCH_TIMEOUT,
            follow_redirects=False,
            headers=headers,
        ) as client:
            async with client.stream("GET", url) as response:
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > MAX_WEBSITE_BYTES:
                    raise WebsiteRecoveryError(
                        "The website page is larger than the 5 MB recovery limit.",
                        code="page_too_large",
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_WEBSITE_BYTES:
                        raise WebsiteRecoveryError(
                            "The website page is larger than the 5 MB recovery limit.",
                            code="page_too_large",
                        )
                return response.status_code, dict(response.headers), bytes(content)
    except WebsiteRecoveryError:
        raise
    except (httpx.TimeoutException, httpx.NetworkError, OSError) as exc:
        raise WebsiteRecoveryError(
            "The website did not respond while VAV was recovering the page.",
            code="fetch_failed",
            retryable=True,
        ) from exc


async def download_public_text(
    url: str,
    *,
    supported_types: tuple[str, ...] = (
        "text/html",
        "application/xhtml",
        "text/plain",
    ),
) -> tuple[str, str, int]:
    """Download a bounded public text resource with DNS-pinned safe redirects."""
    current_url = url
    redirects = 0
    while True:
        status_code = 0
        headers: dict[str, str] = {}
        content = b""
        for attempt in range(FETCH_ATTEMPTS):
            status_code, headers, content = await _download_once(current_url)
            if status_code not in RETRYABLE_STATUS_CODES:
                break
            if attempt < FETCH_ATTEMPTS - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
        if status_code in {301, 302, 303, 307, 308}:
            location = headers.get("location")
            if not location or redirects >= MAX_REDIRECTS:
                raise WebsiteRecoveryError(
                    "The website returned too many or invalid redirects.",
                    code="redirect_failed",
                )
            next_url = urljoin(current_url, location)
            await _resolve_public_destination(next_url)
            current_url = next_url
            redirects += 1
            continue
        if status_code in RETRYABLE_STATUS_CODES:
            raise WebsiteRecoveryError(
                f"The website returned HTTP {status_code} after automatic retries.",
                code="temporary_http_error",
                retryable=True,
            )
        if status_code < 200 or status_code >= 300:
            raise WebsiteRecoveryError(
                f"The website returned HTTP {status_code}.",
                code="http_error",
            )
        content_type = headers.get("content-type", "").lower()
        if not any(value in content_type for value in supported_types):
            raise WebsiteRecoveryError(
                f"The page returned unsupported content type {content_type or 'unknown'}.",
                code="unsupported_content_type",
            )
        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()
        try:
            decoded = content.decode(charset, errors="replace")
        except LookupError:
            decoded = content.decode("utf-8", errors="replace")
        return current_url, decoded, len(content)


async def download_html(url: str) -> tuple[str, str, int]:
    """Download one public HTTPS page with bounded retries and safe redirects."""
    return await download_public_text(url)


def _json_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _json_strings(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _json_strings(child)]
    return []


_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}
_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "canvas", "template", "button"})
_CARD_MAX_CHARS = 600
_CARD_MAX_FRAGMENTS = 14


def _element_signature(element: Tag) -> tuple[str, tuple[str, ...]]:
    classes = element.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    return element.name or "", tuple(sorted(str(value) for value in classes))


def _fragments(element: Tag) -> list[str]:
    return [" ".join(value.split()) for value in element.stripped_strings if value.strip()]


def _looks_like_card(element: Tag, fragments: list[str]) -> bool:
    if element.name in {"table", "thead", "tbody", "ul", "ol", "dl", "tr", "dt", "dd"}:
        return False
    if len(fragments) < 2 or len(fragments) > _CARD_MAX_FRAGMENTS:
        return False
    return sum(len(fragment) for fragment in fragments) <= _CARD_MAX_CHARS


def _table_records(
    table: Tag, *, heading_path: Sequence[str], records: list[KnowledgeRecord]
) -> None:
    header: list[str] = []
    rows = table.find_all("tr")
    for row in rows:
        cells = [cell for cell in row.find_all(["th", "td"], recursive=False)]
        if not cells:
            cells = row.find_all(["th", "td"])
        values = [" ".join(cell.get_text(" ", strip=True).split()) for cell in cells]
        if not any(values):
            continue
        if not header and all(cell.name == "th" for cell in cells):
            header = values
            record = make_record(
                "heading", [RECORD_SEPARATOR.join(values)], heading_path=heading_path
            )
            if record:
                records.append(record)
            continue
        if header and len(header) == len(values):
            values = [
                f"{name}: {value}" if name and value and not value.startswith(f"{name}:") else value
                for name, value in zip(header, values, strict=True)
            ]
        record = make_record("table_row", values, heading_path=heading_path)
        if record:
            records.append(record)


def _walk_records(
    element: Tag,
    *,
    heading_path: list[str],
    records: list[KnowledgeRecord],
    card_signatures: set[tuple[str, tuple[str, ...]]],
) -> None:
    children = [child for child in element.children if isinstance(child, Tag)]
    signatures = Counter(_element_signature(child) for child in children)
    for child in children:
        name = child.name or ""
        if name in _SKIP_TAGS:
            continue
        if name in _HEADING_TAGS:
            text = " ".join(child.get_text(" ", strip=True).split())
            if text:
                level = _HEADING_TAGS[name]
                del heading_path[max(level - 1, 0) :]
                record = make_record("heading", [text], heading_path=heading_path)
                heading_path.append(text)
                if record:
                    records.append(record)
            continue
        if name == "table":
            _table_records(child, heading_path=heading_path, records=records)
            continue
        if name == "dl":
            terms = child.find_all(["dt", "dd"])
            current_term = ""
            for item in terms:
                text = " ".join(item.get_text(" ", strip=True).split())
                if item.name == "dt":
                    current_term = text
                    continue
                record = make_record(
                    "field",
                    [f"{current_term}: {text}" if current_term else text],
                    heading_path=heading_path,
                )
                if record:
                    records.append(record)
            continue
        fragments = _fragments(child)
        if not fragments:
            continue
        repeated = signatures[_element_signature(child)] >= 2
        if name == "tr":
            record = make_record("table_row", fragments, heading_path=heading_path)
            if record:
                records.append(record)
            continue
        if name == "li":
            if child.find(["ul", "ol"]) is not None and len(fragments) > _CARD_MAX_FRAGMENTS:
                _walk_records(
                    child,
                    heading_path=heading_path,
                    records=records,
                    card_signatures=card_signatures,
                )
                continue
            record = make_record(
                "card" if len(fragments) > 1 else "list_item",
                fragments,
                heading_path=heading_path,
            )
            if record:
                records.append(record)
            continue
        if name in {"p", "blockquote", "pre", "figcaption"}:
            record = make_record("paragraph", [" ".join(fragments)], heading_path=heading_path)
            if record:
                records.append(record)
            continue
        signature = _element_signature(child)
        known_card = bool(signature[1]) and signature in card_signatures
        if (repeated or known_card) and _looks_like_card(child, fragments):
            # Sibling components with the same tag and classes are one repeated
            # layout: a doctor card, a price tile, a service block. Keep each
            # one whole so a label shared by several cards is never removed.
            # A layout seen as a card once (on this page or an earlier page of
            # the same listing) stays a card even when it appears alone.
            if signature[1]:
                card_signatures.add(signature)
            record = make_record("card", fragments, heading_path=heading_path)
            if record:
                records.append(record)
            continue
        if child.find(list(_CONTENT_TAGS) + ["table", "dl", "div", "section", "article"]) is None:
            record = make_record(
                "card" if len(fragments) > 1 else "paragraph",
                fragments,
                heading_path=heading_path,
            )
            if record:
                records.append(record)
            continue
        _walk_records(
            child, heading_path=heading_path, records=records, card_signatures=card_signatures
        )


_PAGINATION_MARKERS = ("pagination", "pager", "page-numbers", "page-nav")
_NEXT_LINK_TEXTS = frozenset({"next", "next page", "next »", "next ›", "›", "»", "→", "older"})
MAX_PAGINATED_PAGES = 12


def _is_pagination_control(tag: Tag) -> bool:
    if not isinstance(tag, Tag) or tag.name in {"html", "body", "main", "article"}:
        return False
    classes = tag.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    haystack = " ".join(
        [
            *(str(value) for value in classes),
            str(tag.get("id") or ""),
            str(tag.get("aria-label") or ""),
            str(tag.get("role") or ""),
        ]
    ).casefold()
    return any(marker in haystack for marker in _PAGINATION_MARKERS)


def find_next_page_url(document: str, *, url: str) -> str | None:
    """Return the next page of a paginated listing on the same site, if any."""
    soup = BeautifulSoup(document, "html.parser")
    candidates: list[str] = []
    for tag in soup.find_all(["link", "a"], rel=True):
        rel = tag.get("rel") or []
        if isinstance(rel, str):
            rel = rel.split()
        if "next" in [str(value).casefold() for value in rel] and tag.get("href"):
            candidates.append(str(tag["href"]))
    for anchor in soup.find_all("a", href=True):
        text = " ".join(anchor.get_text(" ", strip=True).split()).casefold()
        label = str(anchor.get("aria-label") or "").casefold()
        if text in _NEXT_LINK_TEXTS or label in _NEXT_LINK_TEXTS:
            candidates.append(str(anchor["href"]))
    current = urlsplit(url)
    for href in candidates:
        resolved = urljoin(url, href)
        parsed = urlsplit(resolved)
        if parsed.scheme != "https" or not parsed.hostname:
            continue
        if parsed.hostname.casefold() != (current.hostname or "").casefold():
            continue
        cleaned = parsed._replace(fragment="").geturl()
        if cleaned != current._replace(fragment="").geturl():
            return cleaned
    return None


def extract_page_records(
    document: str,
    *,
    url: str,
    card_signatures: set[tuple[str, tuple[str, ...]]] | None = None,
) -> tuple[str, list[KnowledgeRecord]]:
    """Extract structured records from static or rendered HTML.

    Repeated sibling components become one record each with their fragments
    kept together; tables become rows; headings carry context. Only whole
    records repeated verbatim are removed, never a label that several records
    legitimately share.  ``card_signatures`` is read and extended so a
    paginated listing recognises a lone card on its last page.
    """
    if card_signatures is None:
        card_signatures = set()
    soup = BeautifulSoup(document, "html.parser")
    title = " ".join((soup.title.get_text(" ", strip=True) if soup.title else "").split())
    description_tag = soup.find("meta", attrs={"name": "description"})
    description = (
        " ".join(str(description_tag.get("content") or "").split()) if description_tag else ""
    )
    structured: list[str] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            structured.extend(_json_strings(json.loads(tag.string or "")))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    for tag in soup.find_all(
        (
            "script",
            "style",
            "noscript",
            "svg",
            "canvas",
            "template",
            "nav",
            "header",
            "footer",
            "aside",
            # Filter bars, search boxes and pagination widgets are controls,
            # not knowledge: "Specialty: All Specialties" is never a fact.
            "form",
            "select",
            "option",
            "input",
            "textarea",
            "button",
        )
    ):
        tag.decompose()
    for tag in soup.find_all(_is_pagination_control):
        tag.decompose()
    semantic_root = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"})
    body_root = soup.body or soup
    if semantic_root is not None:
        semantic_size = len(semantic_root.get_text(" ", strip=True))
        body_size = len(body_root.get_text(" ", strip=True))
        # Some component sites close <main> after the hero and render services,
        # doctors and FAQs as siblings. Prefer the full body when the semantic
        # container would discard a substantial part of the visible page.
        root = semantic_root if semantic_size >= body_size * 0.6 else body_root
    else:
        root = body_root

    records: list[KnowledgeRecord] = []
    if title:
        records.append(KnowledgeRecord(kind="heading", text=title))
    if description:
        record = make_record("paragraph", [description])
        if record:
            records.append(record)
    if isinstance(root, Tag):
        _walk_records(root, heading_path=[], records=records, card_signatures=card_signatures)
    for value in structured:
        record = make_record("field", [value])
        if record:
            records.append(record)
    if sum(len(record.text) for record in records) < STATIC_RENDER_THRESHOLD and isinstance(
        root, Tag
    ):
        # Modern sites often use generic component divs instead of semantic
        # paragraphs. Preserve visible fragments that no record already holds
        # without duplicating a well-structured page's complete body.
        captured = {record.text.casefold() for record in records}
        captured.update(fragment.casefold() for record in records for fragment in record.fragments)
        for value in root.stripped_strings:
            record = make_record("paragraph", [value])
            if record and record.text.casefold() not in captured:
                captured.add(record.text.casefold())
                records.append(record)
    records = dedupe_records(records)
    total = 0
    bounded: list[KnowledgeRecord] = []
    for record in records:
        total += len(record.text) + 2
        if total > MAX_EXTRACTED_CHARS:
            break
        bounded.append(record)
    rendered = render_records(bounded)
    from app.services.knowledge_quality import missing_doctor_directory

    if missing_doctor_directory(title, url, rendered):
        # Trigger the existing automatic browser-rendering recovery even when
        # an empty directory shell contains many SEO/navigation characters.
        # If rendering also fails this source stays failed, never 'indexed'.
        raise WebsiteRecoveryError(
            "Doctor directory entries were not extracted; only page text was found.",
            code="no_readable_text",
        )
    if len(rendered) < MIN_USEFUL_CHARS:
        raise WebsiteRecoveryError(
            "The downloaded page contained too little readable text.",
            code="no_readable_text",
        )
    return title or url, bounded


def extract_readable_text(document: str, *, url: str) -> tuple[str, str]:
    """Extract human-readable content as record-per-paragraph text."""
    title, records = extract_page_records(document, url=url)
    return title, render_records(records)


def should_render_javascript(document: str, text: str) -> bool:
    """Detect client-rendered shells while retaining useful static fallbacks."""
    if len(text) >= STATIC_RENDER_THRESHOLD:
        return False
    lowered = document.casefold()
    markers = (
        'id="app"',
        "id='app'",
        'id="__next"',
        "id='__next'",
        "__next_data__",
        "_next/static",
        "__nuxt",
        "data-reactroot",
    )
    return any(marker in lowered for marker in markers) or lowered.count("<script") >= 4


async def render_html(url: str) -> tuple[str, int]:
    """Render a same-origin JavaScript page in the bundled Chromium fallback."""
    hostname, address = await _resolve_public_destination(url)
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise WebsiteRecoveryError(
            "The JavaScript rendering fallback is unavailable.",
            code="renderer_unavailable",
        ) from exc
    browser = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    f"--host-resolver-rules=MAP {hostname} {address}",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                ],
            )
            page = await browser.new_page(service_workers="block")
            proxied_api_requests = 0

            async def proxy_related_api(route) -> None:
                """Safely relay public same-site APIs used by JavaScript-only pages.

                Chromium is never allowed to resolve these hosts itself. The relay
                validates and pins DNS immediately before the request, retaining the
                SSRF guarantees used by the ordinary website downloader.
                """
                request = route.request
                method = request.method.upper()
                body = request.post_data_buffer or b""
                if method not in {"GET", "POST"} or len(body) > MAX_BROWSER_API_BODY_BYTES:
                    await route.abort()
                    return
                request_hostname, address = await _resolve_public_destination(request.url)
                transport = _PinnedAsyncHTTPTransport(request_hostname, address)
                forwarded_headers = {
                    key: value
                    for key, value in request.headers.items()
                    if key.lower() in {"accept", "content-type", "accept-language"}
                }
                forwarded_headers["User-Agent"] = (
                    "VAV-Knowledge-Recovery/1.0 (+website knowledge indexing)"
                )
                async with httpx.AsyncClient(
                    transport=transport,
                    timeout=FETCH_TIMEOUT,
                    follow_redirects=False,
                    headers=forwarded_headers,
                ) as client:
                    async with client.stream(method, request.url, content=body) as response:
                        content_length = response.headers.get("content-length")
                        if content_length and int(content_length) > MAX_BROWSER_API_BYTES:
                            await route.abort()
                            return
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > MAX_BROWSER_API_BYTES:
                                await route.abort()
                                return
                        response_headers = {
                            key: value
                            for key, value in response.headers.items()
                            if key.lower()
                            not in {
                                "content-encoding",
                                "content-length",
                                "connection",
                                "transfer-encoding",
                            }
                        }
                        response_headers["access-control-allow-origin"] = "*"
                        await route.fulfill(
                            status=response.status_code,
                            headers=response_headers,
                            body=bytes(content),
                        )

            async def guard_request(route):
                nonlocal proxied_api_requests
                parsed = urlsplit(route.request.url)
                if parsed.scheme != "https":
                    await route.abort()
                    return
                if route.request.resource_type in {"image", "media", "font"}:
                    await route.abort()
                    return
                if parsed.hostname == hostname:
                    await route.continue_()
                    return
                if (
                    route.request.resource_type in {"xhr", "fetch"}
                    and _is_related_site_hostname(hostname, parsed.hostname)
                    and proxied_api_requests < MAX_BROWSER_API_REQUESTS
                ):
                    proxied_api_requests += 1
                    try:
                        await proxy_related_api(route)
                    except (WebsiteRecoveryError, httpx.HTTPError, OSError, ValueError):
                        await route.abort()
                    return
                await route.abort()

            await page.route("**/*", guard_request)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                with suppress(PlaywrightTimeoutError):
                    await page.wait_for_load_state("networkidle", timeout=5_000)
                await page.wait_for_timeout(750)
            except PlaywrightTimeoutError:
                # A slow analytics request must not discard already-rendered content.
                pass
            final = urlsplit(page.url)
            if final.scheme != "https" or final.hostname != hostname:
                raise WebsiteRecoveryError(
                    "The rendered page redirected outside its approved website host.",
                    code="unsafe_redirect",
                )
            document = await page.content()
            if len(document.encode("utf-8")) > MAX_WEBSITE_BYTES:
                raise WebsiteRecoveryError(
                    "The rendered page is larger than the 5 MB recovery limit.",
                    code="page_too_large",
                )
            return document, len(document.encode("utf-8"))
    except WebsiteRecoveryError:
        raise
    except Exception as exc:
        raise WebsiteRecoveryError(
            "VAV could not render the JavaScript page.",
            code="render_failed",
            retryable=True,
        ) from exc
    finally:
        if browser is not None:
            with suppress(Exception):
                await browser.close()


async def recover_page(url: str) -> RecoveredPage:
    final_url, static_html, downloaded_bytes = await download_html(url)
    static_page: RecoveredPage | None = None
    try:
        title, text = extract_readable_text(static_html, url=final_url)
        static_page = RecoveredPage(final_url, title, text, "static_html", downloaded_bytes)
        if not should_render_javascript(static_html, text):
            return static_page
    except WebsiteRecoveryError as exc:
        if exc.code != "no_readable_text":
            raise
    try:
        rendered_html, rendered_bytes = await render_html(final_url)
        title, text = extract_readable_text(rendered_html, url=final_url)
        return RecoveredPage(final_url, title, text, "javascript_render", rendered_bytes)
    except WebsiteRecoveryError:
        if static_page is not None:
            return static_page
        raise


def searchable_pdf(*, title: str, url: str, text: str) -> bytes:
    """Create a searchable provider artifact from recovered web text."""
    paragraphs = "".join(f"<p>{html.escape(part)}</p>" for part in text.split("\n\n") if part)
    document_html = (
        f"<h1>{html.escape(title)}</h1><p><b>Source:</b> {html.escape(url)}</p>{paragraphs}"
    )
    output = io.BytesIO()
    writer = pymupdf.DocumentWriter(output)
    page_rect = pymupdf.paper_rect("a4")
    content_rect = page_rect + (42, 42, -42, -42)
    story = pymupdf.Story(
        html=document_html,
        user_css="body { font-family: sans-serif; font-size: 10pt; } h1 { font-size: 16pt; }",
    )
    more = True
    page_count = 0
    while more:
        page_count += 1
        if page_count > 1_000:
            writer.close()
            raise WebsiteRecoveryError(
                "Recovered website content exceeds the provider document limit.",
                code="content_too_large",
            )
        device = writer.begin_page(page_rect)
        more, _filled = story.place(content_rect)
        story.draw(device)
        writer.end_page()
    writer.close()
    value = output.getvalue()
    if not value.startswith(b"%PDF-"):
        raise WebsiteRecoveryError(
            "VAV could not create the searchable provider document.",
            code="pdf_generation_failed",
        )
    if len(value) > MAX_PROVIDER_PDF_BYTES:
        raise WebsiteRecoveryError(
            "The recovered searchable document exceeds the 8 MB provider limit.",
            code="provider_document_too_large",
        )
    return value
