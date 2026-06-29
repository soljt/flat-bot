"""
Live integration tests — opt-in, non-CI.

Each test fetches a real listing from the live platform, verifies that the
parsed Listing has the required email-template fields, renders the email,
extracts the listing URL from the email HTML, and then navigates to that URL
to confirm it resolves to a real listing page (not a 404 / CAPTCHA / error).

Run with:
    uv run pytest -m integration

These tests:
- Open visible Chrome windows (one per browser-based adapter).
- Require FlareSolverr to be running at localhost:8191.
- Are NOT suitable for CI — they depend on live sites and may take minutes.
- Print the listing URL for manual inspection if the assertions pass.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
import logging

import httpx
import pytest

# ── paths ─────────────────────────────────────────────────────────────────────
sys.path.insert(0, str(__file__.rsplit("tests", 1)[0]))

from flatbot.adapters.cloudflare import FlareSolverrSession
from flatbot.adapters.flatfox import FlatfoxAdapter
from flatbot.adapters.homegate import HomegateAdapter
from flatbot.adapters.immoscout import ImmoScout24Adapter
from flatbot.adapters.newhome import NewHomeAdapter
from flatbot.adapters.comparis import ComparisAdapter
from flatbot.adapters.base import Listing
from flatbot.llm import fallback_body

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ── constants ─────────────────────────────────────────────────────────────────

_FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "http://localhost:8191/v1")
_MIN_ROOMS = 5.0
_MIN_RENT = 3000.0
_MAX_RENT = 6500.0

# ── helpers ───────────────────────────────────────────────────────────────────

def _session() -> FlareSolverrSession:
    return FlareSolverrSession(
        flaresolverr_url=_FLARESOLVERR_URL,
        max_timeout_ms=60_000,
    )


def _first_href(body: str) -> str:
    m = re.search(r'href="([^"]+)"', body)
    assert m, f"No href in rendered email body: {body[:300]!r}"
    return m.group(1)


def _assert_listing_fields(listing: Listing, *, expect_available_from: bool) -> None:
    """Assert that all email-template fields that must be populated, are."""
    assert listing.id, "listing.id must be non-empty"
    assert listing.title, "listing.title must be non-empty"
    assert listing.rooms is not None, "listing.rooms must be set"
    assert listing.url, "listing.url must be non-empty"
    assert listing.postcode or listing.address, (
        "at least one of postcode or address must be set for geographic context"
    )
    assert (
        listing.price_chf is not None or listing.price_on_request or listing.price_is_teaser
    ), "at least one price field must be set"
    if expect_available_from:
        assert listing.available_from is not None, (
            f"platform={listing.platform} expected available_from to be set"
        )


def _verify_flatfox_url(url: str, listing_id: str) -> None:
    """Verify a Flatfox listing URL resolves to the correct page via plain httpx."""
    with httpx.Client(
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
        },
        timeout=20,
        follow_redirects=True,
    ) as client:
        resp = client.get(url)
    assert resp.status_code == 200, (
        f"Flatfox listing URL returned HTTP {resp.status_code}: {url}"
    )
    # The listing ID should appear in the final URL (after any slug redirect).
    assert listing_id in str(resp.url), (
        f"Listing id {listing_id!r} not found in final URL {resp.url!r}"
    )


async def _verify_browser_url_async(url: str, listing_id: str, platform: str) -> None:
    """
    Open a nodriver Chrome window, navigate to the listing URL, and assert
    it resolves to the actual listing (not a 404 / error / CAPTCHA page).
    """
    import nodriver as uc  # type: ignore[import]

    extra_args: list[str] = ["--disable-dev-shm-usage"]
    if os.path.exists("/.dockerenv"):
        extra_args.append("--no-sandbox")

    browser = await uc.start(
        headless=False,
        browser_executable_path=os.getenv("CHROME_EXECUTABLE_PATH") or None,
        browser_args=extra_args,
    )
    try:
        tab = await browser.get(url)
        # Give the page time to load (including any CF/DataDome challenge).
        await asyncio.sleep(6)

        # Check the title for obvious error indicators.
        page_title: str = await tab.evaluate("document.title") or ""
        assert "404" not in page_title.lower(), (
            f"platform={platform}: page title suggests 404: {page_title!r} — URL: {url}"
        )
        assert "not found" not in page_title.lower(), (
            f"platform={platform}: page title suggests not-found: {page_title!r} — URL: {url}"
        )
        assert "error" not in page_title.lower() or platform == "comparis", (
            f"platform={platform}: page title suggests error: {page_title!r} — URL: {url}"
        )

        # The listing id should appear either in the final URL or in the page source.
        final_url: str = await tab.evaluate("window.location.href") or ""
        page_html: str = await tab.evaluate("document.documentElement.innerHTML") or ""

        assert listing_id in final_url or listing_id in page_html, (
            f"platform={platform}: listing id {listing_id!r} not found in "
            f"final URL {final_url!r} or page content (first 500 chars: {page_html[:500]!r})"
        )

        print(f"\n  [{platform}] URL resolved OK: {final_url}", flush=True)
    finally:
        browser.stop()
        await asyncio.sleep(0.5)


def _verify_browser_url(url: str, listing_id: str, platform: str) -> None:
    """Synchronous wrapper around _verify_browser_url_async."""
    asyncio.run(_verify_browser_url_async(url, listing_id, platform))


# ── Flatfox ───────────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_flatfox_live():
    """Fetch a real Flatfox listing, verify all fields, render email, verify link."""
    adapter = FlatfoxAdapter(min_rooms=_MIN_ROOMS, max_rent_chf=_MAX_RENT)
    listings = adapter.search()
    assert listings, "Flatfox returned no listings — is the site down or are filters too strict?"

    listing = listings[0]
    print(f"\n  [flatfox] id={listing.id}, url={listing.url}", flush=True)

    _assert_listing_fields(listing, expect_available_from=False)  # not all listings have it

    body = fallback_body(listing)
    href = _first_href(body)
    assert href == listing.url, f"Email href {href!r} != listing.url {listing.url!r}"

    _verify_flatfox_url(href, listing.id)


# ── Homegate ──────────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_homegate_live():
    """Fetch a real Homegate listing, verify fields, render email, verify link."""
    adapter = HomegateAdapter(min_rooms=_MIN_ROOMS, max_rent_chf=_MAX_RENT, session=_session())
    listings = adapter.search()
    assert listings, "Homegate returned no listings"

    listing = listings[0]
    print(f"\n  [homegate] id={listing.id}, url={listing.url}", flush=True)

    # Homegate does not expose availableFrom in its search API.
    _assert_listing_fields(listing, expect_available_from=False)
    assert listing.available_from is None, "Homegate should not set available_from (not in API)"

    # Verify detail-page enrichment: get_available_from opens the listing URL
    # in Chrome and extracts the date from the JSON blob embedded in the HTML.
    avail = adapter.get_available_from(listing.url)
    print(f"  [homegate] detail available_from={avail!r}", flush=True)
    assert avail is None or isinstance(avail, str), (
        f"get_available_from must return str or None, got {type(avail)}"
    )

    body = fallback_body(listing)
    href = _first_href(body)
    assert href == listing.url

    _verify_browser_url(href, listing.id, "homegate")


# ── ImmoScout24 ───────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_immoscout_live():
    """Fetch a real ImmoScout24 listing, verify fields, render email, verify link."""
    adapter = ImmoScout24Adapter(
        min_rooms=_MIN_ROOMS,
        min_rent_chf=_MIN_RENT,
        max_rent_chf=_MAX_RENT,
        session=_session(),
    )
    listings = adapter.search()
    assert listings, "ImmoScout24 returned no listings"

    listing = listings[0]
    print(f"\n  [immoscout24] id={listing.id}, url={listing.url}", flush=True)

    # IS24 shares the same SMG schema as Homegate; availableFrom not in API.
    _assert_listing_fields(listing, expect_available_from=False)
    assert listing.available_from is None, "IS24 should not set available_from (not in API)"

    # Verify detail-page enrichment (same HTML regex as Homegate — shared SMG backend).
    avail = adapter.get_available_from(listing.url)
    print(f"  [immoscout24] detail available_from={avail!r}", flush=True)
    assert avail is None or isinstance(avail, str), (
        f"get_available_from must return str or None, got {type(avail)}"
    )

    body = fallback_body(listing)
    href = _first_href(body)
    assert href == listing.url

    _verify_browser_url(href, listing.id, "immoscout24")


# ── NewHome ───────────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_newhome_live():
    """Fetch a real NewHome listing, verify fields including available_from, verify link."""
    adapter = NewHomeAdapter(
        min_rooms=_MIN_ROOMS,
        min_rent_chf=_MIN_RENT,
        max_rent_chf=_MAX_RENT,
        session=_session(),
    )
    listings = adapter.search()
    assert listings, "NewHome returned no listings"

    # Find a listing with available_from set if possible; fall back to first.
    listing = next((l for l in listings if l.available_from), listings[0])
    print(
        f"\n  [newhome] id={listing.id}, url={listing.url}, "
        f"available_from={listing.available_from}",
        flush=True,
    )

    _assert_listing_fields(listing, expect_available_from=False)  # not guaranteed on all
    # If available_from is set, it must be a YYYY-MM-DD string.
    if listing.available_from:
        assert re.match(r"\d{4}-\d{2}-\d{2}", listing.available_from), (
            f"available_from should be YYYY-MM-DD, got {listing.available_from!r}"
        )

    body = fallback_body(listing)
    href = _first_href(body)
    assert href == listing.url

    _verify_browser_url(href, str(listing.id), "newhome")


# ── Comparis ──────────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_comparis_live():
    """Fetch a real Comparis listing, verify fields, render email, verify link."""
    adapter = ComparisAdapter(
        min_rooms=_MIN_ROOMS,
        min_rent_chf=_MIN_RENT,
        max_rent_chf=_MAX_RENT,
        session=_session(),
    )
    listings = adapter.search()
    assert listings, "Comparis returned no listings"

    listing = listings[0]
    print(f"\n  [comparis] id={listing.id}, url={listing.url}", flush=True)

    # Comparis search results do not expose per-listing availability dates.
    _assert_listing_fields(listing, expect_available_from=False)
    assert listing.available_from is None, (
        "Comparis does not expose available_from in search results; expected None"
    )
    assert isinstance(listing.description, str), (
        "listing.description must be a str, not None (regression for empty-Remarks bug)"
    )

    # Verify detail-page enrichment: get_available_from reads AvailableDate from
    # __NEXT_DATA__.props.pageProps.ad.MainData on the Comparis detail page.
    avail = adapter.get_available_from(listing.url)
    print(f"  [comparis] detail available_from={avail!r}", flush=True)
    assert avail is None or isinstance(avail, str), (
        f"get_available_from must return str or None, got {type(avail)}"
    )

    body = fallback_body(listing)
    href = _first_href(body)
    assert href == listing.url

    _verify_browser_url(href, listing.id, "comparis")
