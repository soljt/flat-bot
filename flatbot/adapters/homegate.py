from __future__ import annotations

"""
Homegate adapter — scrapes listings from the Homegate search HTML page.

Background
----------
Homegate has no usable public JSON API: ``api.homegate.ch`` is protected by
DataDome.  The search results page is server-side rendered by Vue/Nuxt and
embeds listing data in ``window.__INITIAL_STATE__`` inside a <script> tag.

Transport
---------
Plain HTTP (httpx, curl-cffi) and FlareSolverr-warmed cookie reuse are both
blocked by DataDome on the search pages.  ``nodriver`` launches a real Chrome
browser via a non-CDP protocol that DataDome cannot detect, and passes all
challenges automatically.

Search URL
----------
  https://www.homegate.ch/rent/real-estate/city-zurich/matching-list
    ?ep=<page>   — page number (1-indexed)
    &ac=<rooms>  — minimum room count  (ac = Anzahl Zimmer)
    &ag=<price>  — minimum gross rent in CHF
    &ah=<price>  — maximum gross rent in CHF
    &o=dateCreated-desc — sort newest-first (default is topListing/relevance)

  NOTE: `al` is NOT price — it is the max living surface (m²). An earlier
  version used `al` for max rent, so Homegate never actually price-filtered
  (the pipeline's own filter masked it). Price is `ag`/`ah`.

JSON shape (window.__INITIAL_STATE__)
--------------------------------------
  resultList.search.fullSearch.result.listings   — array of listing wrappers
  resultList.search.fullSearch.result.pageCount  — total pages

Each listing wrapper has the listing nested under a "listing" key.
"""

import asyncio
import json
import logging
import math
import os
import random
import re
import time
from urllib.parse import urlencode

import nodriver as uc

# In Docker containers Chrome must run with --no-sandbox (root user, no kernel
# namespace support).  Detect by checking for the Docker sentinel file.
_IN_DOCKER = os.path.exists("/.dockerenv")
_CHROME_BIN = os.getenv("CHROME_EXECUTABLE_PATH") or None

from collections.abc import Callable

from .base import (
    Adapter,
    Listing,
    detect_no_wg,
    detect_price_on_request,
    detect_teaser_price,
    page_all_seen,
)

log = logging.getLogger(__name__)

_BASE_SEARCH_URL = (
    "https://www.homegate.ch/rent/real-estate/city-zurich/matching-list"
)

_MAX_PAGES = 10
_STATE_TIMEOUT_S = 30  # seconds to wait for __INITIAL_STATE__ per page


def _created_at(raw: dict) -> str | None:
    """Publication timestamp (ISO8601-Z) from a raw Homegate listing, or None.

    Used only to verify that the result set is ordered newest-first before we
    trust the seen-based early-exit. ISO8601-Z strings sort lexicographically.
    """
    lst = raw.get("listing") or raw
    meta = lst.get("meta") if isinstance(lst, dict) else None
    val = (meta or {}).get("createdAt")
    return val if isinstance(val, str) else None


class HomegateAdapter(Adapter):
    name = "homegate"

    def __init__(
        self,
        min_rooms: float,
        max_rent_chf: float,
        min_rent_chf: float = 0.0,
        session=None,  # kept for interface compatibility; not used
    ) -> None:
        self._min_rooms = min_rooms
        self._min_rent_chf = min_rent_chf
        self._max_rent_chf = max_rent_chf

    def search(self, is_seen: Callable[[str], bool] | None = None) -> list[Listing]:
        try:
            return asyncio.run(self._async_search(is_seen))
        except Exception as exc:
            log.error("platform=homegate action=search_failed error=%r", str(exc))
            return []

    def get_available_from(self, url: str) -> str | None:
        try:
            return asyncio.run(self._async_get_available_from(url))
        except Exception as exc:
            log.warning("platform=homegate action=detail_fetch_failed url=%s error=%r", url, str(exc))
            return None

    # ── async implementation ─────────────────────────────────────────────────

    async def _async_get_available_from(self, url: str) -> str | None:
        extra_args = ["--disable-dev-shm-usage"]
        if _IN_DOCKER:
            extra_args.append("--no-sandbox")
        browser = await uc.start(
            headless=False,
            browser_executable_path=_CHROME_BIN,
            browser_args=extra_args,
        )
        try:
            tab = await browser.get(url)
            await asyncio.sleep(5)
            html = await tab.evaluate("(function() { return document.documentElement.innerHTML; })()")
            return _extract_available_from_html(html or "")
        finally:
            browser.stop()
            await asyncio.sleep(0.5)

    async def _async_search(
        self, is_seen: Callable[[str], bool] | None = None
    ) -> list[Listing]:
        extra_args = ["--disable-dev-shm-usage"]
        if _IN_DOCKER:
            extra_args.append("--no-sandbox")
        browser = await uc.start(
            headless=False,
            browser_executable_path=_CHROME_BIN,
            browser_args=extra_args,
        )
        try:
            return await self._fetch_pages(browser, is_seen)
        finally:
            browser.stop()
            # Let the event loop drain subprocess transports before asyncio.run()
            # closes the loop; avoids "I/O operation on closed pipe" noise on Windows.
            await asyncio.sleep(0.5)

    def _build_url(self, page: int) -> str:
        params = {
            "ep": str(page),                        # page number (1-indexed)
            "ac": str(math.floor(self._min_rooms)),  # min rooms (Anzahl Zimmer)
            "ag": str(int(self._min_rent_chf)),     # min gross rent CHF
            "ah": str(int(self._max_rent_chf)),     # max gross rent CHF
            # Sort newest-first so the seen-based early-exit is valid. Without
            # this Homegate defaults to sortType=topListing (promoted/relevance),
            # whose createdAt order is scrambled and trips the newest-first guard.
            "o": "dateCreated-desc",
        }
        return f"{_BASE_SEARCH_URL}?{urlencode(params)}"

    async def _wait_for_state(self, tab) -> tuple[list[dict], int] | None:
        """Poll until __INITIAL_STATE__ is populated; extract listings + pageCount."""
        deadline = asyncio.get_event_loop().time() + _STATE_TIMEOUT_S
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await tab.evaluate("""
                    (function() {
                        var s = window.__INITIAL_STATE__;
                        if (!s) return null;
                        var r = s.resultList && s.resultList.search
                                && s.resultList.search.fullSearch
                                && s.resultList.search.fullSearch.result;
                        if (!r) return null;
                        return JSON.stringify({listings: r.listings, pageCount: r.pageCount});
                    })()
                """)
                if raw and raw not in ("null", "undefined", ""):
                    parsed = json.loads(raw)
                    return parsed.get("listings", []), int(parsed.get("pageCount", 1))
            except Exception:
                pass
            await asyncio.sleep(1)
        return None

    async def _fetch_pages(
        self, browser, is_seen: Callable[[str], bool] | None = None
    ) -> list[Listing]:
        listings: list[Listing] = []
        tab = None
        newest_first = True       # verified per page via meta.createdAt
        prev_page_oldest = None   # oldest createdAt seen on the previous page

        for page_num in range(1, _MAX_PAGES + 1):
            url = self._build_url(page_num)
            try:
                if tab is None:
                    tab = await browser.get(url)
                else:
                    await tab.get(url)
            except Exception as exc:
                log.error(
                    "platform=homegate action=navigate_error page=%d error=%r",
                    page_num, str(exc),
                )
                break

            result = await self._wait_for_state(tab)
            if result is None:
                log.warning(
                    "platform=homegate action=no_initial_state page=%d — "
                    "challenge not cleared or page structure changed",
                    page_num,
                )
                break

            raw_listings, page_count = result

            # Verify newest-first ordering before trusting the early-exit: if a
            # later page carries a listing newer than the previous page's oldest,
            # the results are not newest-first and early-exit would skip new flats.
            created = sorted(c for c in (_created_at(r) for r in raw_listings) if c)
            if created:
                if newest_first and prev_page_oldest is not None and created[-1] > prev_page_oldest:
                    newest_first = False
                    log.warning(
                        "platform=homegate action=order_not_newest_first page=%d — "
                        "disabling seen-based early-exit for this run",
                        page_num,
                    )
                prev_page_oldest = created[0]

            page_listings: list[Listing] = []
            for raw in raw_listings:
                try:
                    listing = _parse(raw)
                    if listing:
                        page_listings.append(listing)
                except Exception:
                    raw_id = (raw.get("listing") or raw).get("id", "?")
                    log.warning(
                        "platform=homegate action=item_parse_error id=%s",
                        raw_id,
                        exc_info=True,
                    )
            listings.extend(page_listings)

            log.info(
                "platform=homegate action=page_fetched page=%d/%d listings_so_far=%d",
                page_num, page_count, len(listings),
            )

            if newest_first and page_all_seen(page_listings, is_seen):
                log.info(
                    "platform=homegate action=early_exit page=%d reason=all_seen count=%d",
                    page_num, len(listings),
                )
                break

            if page_num >= page_count:
                break
            await asyncio.sleep(random.uniform(1.0, 2.5))

        log.info("platform=homegate action=fetched count=%d", len(listings))
        return listings


def _extract_available_from_html(html: str) -> str | None:
    """Extract availability from the rendered Homegate detail page.

    Primary source: the structured key-value pair rendered in the page body:
      <dt>Available from:</dt><dd>By agreement</dd>
      <dt>Available from:</dt><dd>01.09.2026</dd>
      <dt>Available from:</dt><dd>Immediately</dd>

    Returns the value verbatim so the caller sees the exact label the site uses.
    Falls back to a JSON-embedded ISO date for future-proofing.
    """
    m = re.search(r"<dt>Available from:</dt>\s*<dd>([^<]+)</dd>", html, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        return val or None
    m = re.search(r'"availableFrom"\s*:\s*"(\d{4}-\d{2}-\d{2})', html)
    return m.group(1) if m else None


def _parse(item: dict) -> Listing | None:
    d = item.get("listing", item)

    listing_id = str(d.get("id", "")).strip()
    if not listing_id:
        return None

    loc = d.get("localization", {})
    lang = loc.get("de") or loc.get("en") or {}
    text = lang.get("text", {}) if isinstance(lang.get("text"), dict) else {}
    title = text.get("title") or lang.get("title") or d.get("title") or ""
    description = text.get("description") or lang.get("description") or d.get("description") or ""
    full_text = f"{title} {description}"

    prices = d.get("prices", {})
    rent = prices.get("rent", {})
    gross = rent.get("gross")
    price_chf = float(gross) if gross is not None else None
    price_is_teaser = bool(rent.get("isFrom")) or detect_teaser_price(full_text)
    price_on_request = price_chf is None and detect_price_on_request(full_text)

    chars = d.get("characteristics", {})
    rooms_raw = chars.get("numberOfRooms")
    rooms = float(rooms_raw) if rooms_raw is not None else None

    addr = d.get("address", {})
    postcode = str(addr.get("postalCode") or "").strip()
    city = addr.get("city") or addr.get("locality") or ""
    street = addr.get("street") or ""
    house_no = addr.get("houseNumber") or ""
    street_full = f"{street} {house_no}".strip()
    address_parts = [p for p in [street_full, f"{postcode} {city}".strip()] if p]
    address = ", ".join(address_parts) or None

    available_from = d.get("availableFrom")

    url = f"https://www.homegate.ch/rent/{listing_id}"

    if not title:
        title = f"{rooms or '?'}R Zürich {postcode}"

    return Listing(
        id=listing_id,
        url=url,
        title=title,
        price_chf=price_chf,
        rooms=rooms,
        postcode=postcode or None,
        address=address,
        available_from=str(available_from) if available_from else None,
        description=description,
        platform="homegate",
        price_is_teaser=price_is_teaser,
        price_on_request=price_on_request,
        no_wg_clause=detect_no_wg(full_text),
    )
