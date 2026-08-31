import pytest

from app.services import website_crawler
from app.services.website_crawler import (
    canonicalize_page_url,
    discover_website,
    extract_page_links,
)


def test_canonicalize_page_url_removes_trackers_and_rejects_non_pages():
    assert (
        canonicalize_page_url("https://EXAMPLE.com/services/?utm_source=test&category=skin#prices")
        == "https://example.com/services?category=skin"
    )
    assert canonicalize_page_url("https://example.com/brochure.pdf") is None
    assert canonicalize_page_url("https://example.com/checkout") is None
    assert canonicalize_page_url("http://example.com/services") is None


def test_extract_page_links_deduplicates_and_ignores_unsafe_or_nofollow_links():
    links = extract_page_links(
        """
        <a href="/services/">Services</a>
        <a href="https://clinic.example/services?utm_campaign=x">Duplicate</a>
        <a href="/private" rel="nofollow">Private</a>
        <a href="mailto:hello@clinic.example">Email</a>
        <a href="/guide.pdf">PDF</a>
        """,
        base_url="https://clinic.example/",
    )

    assert links == ["https://clinic.example/services"]


@pytest.mark.asyncio
async def test_discover_website_combines_sitemap_and_same_site_links(monkeypatch):
    documents = {
        "https://clinic.example/": """
            <html><body>
              <a href="/services">Services</a>
              <a href="https://external.example/tracker">External</a>
              <a href="/checkout">Checkout</a>
            </body></html>
        """,
        "https://clinic.example/services": """
            <html><body><a href="/offers">Offers</a></body></html>
        """,
        "https://clinic.example/doctors": "<html><body><p>Doctors</p></body></html>",
        "https://clinic.example/offers": "<html><body><p>Offers</p></body></html>",
    }

    async def download(url):
        return url, documents[url], len(documents[url])

    class Robots:
        def can_fetch(self, _agent, url):
            return not url.endswith("/private")

        def crawl_delay(self, _agent):
            return None

    async def robots(_root):
        return Robots(), ["https://clinic.example/sitemap.xml"], []

    async def sitemap(_urls, **_kwargs):
        return ["https://clinic.example/doctors"], []

    monkeypatch.setattr(website_crawler, "download_html", download)
    monkeypatch.setattr(website_crawler, "_load_robots", robots)
    monkeypatch.setattr(website_crawler, "_discover_sitemap_pages", sitemap)

    result = await discover_website(
        "https://clinic.example/",
        max_pages=20,
        max_depth=3,
        include_subdomains=False,
    )

    assert result.allowed_host == "clinic.example"
    assert [page.canonical_url for page in result.pages] == [
        "https://clinic.example/",
        "https://clinic.example/doctors",
        "https://clinic.example/services",
        "https://clinic.example/offers",
    ]
    assert [page.discovered_via for page in result.pages] == [
        "homepage",
        "sitemap",
        "link",
        "link",
    ]
