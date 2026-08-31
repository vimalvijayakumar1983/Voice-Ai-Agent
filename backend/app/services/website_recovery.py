"""Safe, provider-independent recovery for failed website knowledge sources."""

from __future__ import annotations

import asyncio
import html
import io
import ipaddress
import json
import socket
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx
import pymupdf
from bs4 import BeautifulSoup

from app.services.integration_security import IntegrationConfigError, validate_public_https_url

MAX_WEBSITE_BYTES = 5 * 1024 * 1024
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


async def download_html(url: str) -> tuple[str, str, int]:
    """Download one public HTTPS page with bounded retries and safe redirects."""
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
        supported_types = ("text/html", "application/xhtml", "text/plain")
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


def _json_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _json_strings(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _json_strings(child)]
    return []


def extract_readable_text(document: str, *, url: str) -> tuple[str, str]:
    """Extract de-duplicated human-readable content from static or rendered HTML."""
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
    for tag in soup.find_all(("script", "style", "noscript", "svg", "canvas", "template")):
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
    lines = [title, description]
    lines.extend(element.get_text(" ", strip=True) for element in root.find_all(_CONTENT_TAGS))
    lines.extend(structured)
    if sum(len(value) for value in lines) < STATIC_RENDER_THRESHOLD:
        # Modern sites often use generic component divs instead of semantic
        # paragraphs. Preserve their visible fragments without duplicating a
        # well-structured page's complete body.
        lines.extend(root.stripped_strings)
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in lines:
        normalized = " ".join(value.split()).strip()
        if len(normalized) < 2:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(normalized)
    text = "\n\n".join(cleaned)[:MAX_EXTRACTED_CHARS]
    if len(text) < MIN_USEFUL_CHARS:
        raise WebsiteRecoveryError(
            "The downloaded page contained too little readable text.",
            code="no_readable_text",
        )
    return title or url, text


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

            async def guard_request(route):
                parsed = urlsplit(route.request.url)
                if parsed.scheme != "https" or parsed.hostname != hostname:
                    await route.abort()
                    return
                if route.request.resource_type in {"image", "media", "font"}:
                    await route.abort()
                    return
                await route.continue_()

            await page.route("**/*", guard_request)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(1_500)
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
