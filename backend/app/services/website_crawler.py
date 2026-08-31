"""Bounded, same-site discovery for fully automatic knowledge crawling."""

from __future__ import annotations

import asyncio
import re
from collections import deque
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from app.services.website_recovery import (
    WebsiteRecoveryError,
    download_html,
    download_public_text,
    render_html,
)

CRAWLER_USER_AGENT = "VAV-Knowledge-Recovery"
MAX_CRAWL_PAGES = 500
MAX_SITEMAP_DOCUMENTS = 20
MAX_SITEMAP_DEPTH = 3
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid"}
BLOCKED_EXTENSIONS = {
    ".7z",
    ".avi",
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".m4a",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".rss",
    ".svg",
    ".tar",
    ".webp",
    ".woff",
    ".woff2",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}
BLOCKED_PATH_SEGMENTS = {
    "admin",
    "cart",
    "checkout",
    "login",
    "logout",
    "my-account",
    "register",
    "signin",
    "signup",
    "wp-admin",
    "wp-login.php",
}


@dataclass(frozen=True)
class DiscoveredPage:
    url: str
    canonical_url: str
    depth: int
    discovered_via: str


@dataclass(frozen=True)
class CrawlDiscovery:
    root_url: str
    allowed_host: str
    pages: tuple[DiscoveredPage, ...]
    skipped_count: int
    warnings: tuple[str, ...]


def canonicalize_page_url(value: str) -> str | None:
    """Return a stable HTTPS page URL while removing fragments and trackers."""
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = host if port in {None, 443} else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/") or "/"
    suffix = path.rsplit("/", 1)[-1].lower()
    if any(suffix.endswith(extension) for extension in BLOCKED_EXTENSIONS):
        return None
    segments = {segment.casefold() for segment in path.split("/") if segment}
    if segments & BLOCKED_PATH_SEGMENTS:
        return None
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in TRACKING_QUERY_KEYS
    ]
    return urlunsplit(("https", netloc, path, urlencode(sorted(query)), ""))


def is_allowed_host(candidate: str, allowed_host: str, *, include_subdomains: bool) -> bool:
    host = (urlsplit(candidate).hostname or "").rstrip(".").lower()
    allowed = allowed_host.rstrip(".").lower()
    return host == allowed or (include_subdomains and host.endswith(f".{allowed}"))


def extract_page_links(document: str, *, base_url: str) -> list[str]:
    soup = BeautifulSoup(document, "html.parser")
    values: list[str] = []
    for tag in soup.find_all("a", href=True):
        rel = {str(value).casefold() for value in (tag.get("rel") or [])}
        if "nofollow" in rel:
            continue
        href = str(tag.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        canonical = canonicalize_page_url(urljoin(base_url, href))
        if canonical:
            values.append(canonical)
    return list(dict.fromkeys(values))


def _sitemap_locations(document: str) -> tuple[list[str], list[str]]:
    """Return (page URLs, nested sitemap URLs) from a bounded XML document."""
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise WebsiteRecoveryError(
            "The website sitemap is not valid XML.", code="invalid_sitemap"
        ) from exc
    tag = root.tag.rsplit("}", 1)[-1].casefold()
    locations = [
        "".join(element.itertext()).strip()
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1].casefold() == "loc"
    ]
    if tag == "sitemapindex":
        return [], locations
    return locations, []


async def _load_robots(root_url: str) -> tuple[RobotFileParser, list[str], list[str]]:
    parsed = urlsplit(root_url)
    robots_url = urlunsplit(("https", parsed.netloc, "/robots.txt", "", ""))
    parser = RobotFileParser()
    parser.set_url(robots_url)
    warnings: list[str] = []
    sitemaps: list[str] = []
    try:
        _final, document, _size = await download_public_text(
            robots_url,
            supported_types=("text/plain", "text/html"),
        )
        parser.parse(document.splitlines())
        sitemaps = [value.strip() for value in parser.site_maps() or [] if value.strip()]
    except WebsiteRecoveryError as exc:
        if exc.code not in {"http_error"}:
            warnings.append(
                "robots.txt could not be read; public pages were discovered cautiously."
            )
        parser.parse([])
    standard = urlunsplit(("https", parsed.netloc, "/sitemap.xml", "", ""))
    if standard not in sitemaps:
        sitemaps.append(standard)
    return parser, sitemaps, warnings


async def _discover_sitemap_pages(
    sitemap_urls: list[str],
    *,
    allowed_host: str,
    include_subdomains: bool,
    limit: int,
) -> tuple[list[str], list[str]]:
    pages: list[str] = []
    warnings: list[str] = []
    queue = deque((url, 0) for url in sitemap_urls)
    seen: set[str] = set()
    while queue and len(seen) < MAX_SITEMAP_DOCUMENTS and len(pages) < limit:
        sitemap_url, depth = queue.popleft()
        # XML is intentionally excluded by page canonicalization.
        sitemap_key = sitemap_url.split("#", 1)[0].strip()
        if not sitemap_key.startswith("https://") or sitemap_key in seen:
            continue
        if not is_allowed_host(sitemap_key, allowed_host, include_subdomains=include_subdomains):
            continue
        seen.add(sitemap_key)
        try:
            _final, document, _size = await download_public_text(
                sitemap_key,
                supported_types=(
                    "application/xml",
                    "text/xml",
                    "application/rss+xml",
                    "text/plain",
                ),
            )
            page_urls, nested = _sitemap_locations(document)
        except WebsiteRecoveryError:
            if depth == 0:
                warnings.append(f"Sitemap unavailable: {sitemap_key}")
            continue
        for value in page_urls:
            canonical = canonicalize_page_url(value)
            if canonical and is_allowed_host(
                canonical, allowed_host, include_subdomains=include_subdomains
            ):
                pages.append(canonical)
                if len(pages) >= limit:
                    break
        if depth < MAX_SITEMAP_DEPTH:
            queue.extend((value, depth + 1) for value in nested)
    return list(dict.fromkeys(pages))[:limit], warnings


async def discover_website(
    root_url: str,
    *,
    max_pages: int,
    max_depth: int,
    include_subdomains: bool,
) -> CrawlDiscovery:
    """Discover a bounded same-site page set using sitemaps plus link traversal."""
    if max_pages < 1 or max_pages > MAX_CRAWL_PAGES:
        raise ValueError(f"max_pages must be between 1 and {MAX_CRAWL_PAGES}")
    canonical_root = canonicalize_page_url(root_url)
    if canonical_root is None:
        raise WebsiteRecoveryError("Enter a public HTTPS homepage URL.", code="invalid_url")

    final_root, root_document, _size = await download_html(canonical_root)
    canonical_root = canonicalize_page_url(final_root)
    if canonical_root is None:
        raise WebsiteRecoveryError(
            "The homepage redirected to an invalid page.", code="invalid_url"
        )
    allowed_host = (urlsplit(canonical_root).hostname or "").lower()
    if include_subdomains and allowed_host.startswith("www."):
        allowed_host = allowed_host[4:]
    robots, sitemap_urls, warnings = await _load_robots(canonical_root)
    sitemap_pages, sitemap_warnings = await _discover_sitemap_pages(
        sitemap_urls,
        allowed_host=allowed_host,
        include_subdomains=include_subdomains,
        limit=max_pages,
    )
    warnings.extend(sitemap_warnings)

    discovered: dict[str, DiscoveredPage] = {}
    queue: deque[tuple[str, int, str, str | None]] = deque()
    queue.append((canonical_root, 0, "homepage", root_document))
    queue.extend((url, 1, "sitemap", None) for url in sitemap_pages if url != canonical_root)
    visited_for_links: set[str] = set()
    skipped = 0
    crawl_delay = min(max(float(robots.crawl_delay(CRAWLER_USER_AGENT) or 0), 0), 5)

    while queue and len(discovered) < max_pages:
        url, depth, via, supplied_document = queue.popleft()
        canonical = canonicalize_page_url(url)
        if canonical is None or canonical in discovered:
            continue
        if not is_allowed_host(canonical, allowed_host, include_subdomains=include_subdomains):
            skipped += 1
            continue
        if not robots.can_fetch(CRAWLER_USER_AGENT, canonical):
            skipped += 1
            continue
        discovered[canonical] = DiscoveredPage(canonical, canonical, depth, via)
        if depth >= max_depth or canonical in visited_for_links:
            continue
        visited_for_links.add(canonical)
        try:
            if supplied_document is None:
                final_url, document, _size = await download_html(canonical)
                if canonicalize_page_url(final_url) != canonical:
                    supplied_document = document
                else:
                    supplied_document = document
            document = supplied_document
            links = extract_page_links(document, base_url=canonical)
            if len(links) < 2 and any(
                marker in document.casefold()
                for marker in ('id="__next"', "id='__next'", 'id="app"', "id='app'", "__nuxt")
            ):
                rendered, _rendered_size = await render_html(canonical)
                links = extract_page_links(rendered, base_url=canonical)
        except WebsiteRecoveryError:
            # The page remains in the ledger and the recovery worker will retry,
            # render, classify, and expose the exact failure independently.
            continue
        for link in links:
            if link not in discovered:
                queue.append((link, depth + 1, "link", None))
        await asyncio.sleep(crawl_delay)

    if queue:
        warnings.append(f"Discovery stopped at the configured {max_pages}-page safety limit.")
    return CrawlDiscovery(
        root_url=canonical_root,
        allowed_host=allowed_host,
        pages=tuple(discovered.values()),
        skipped_count=skipped,
        warnings=tuple(dict.fromkeys(warnings)),
    )
