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
    &al=<price>  — maximum gross rent in CHF

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
import time
from urllib.parse import urlencode

import nodriver as uc

# In Docker containers Chrome must run with --no-sandbox (root user, no kernel
# namespace support).  Detect by checking for the Docker sentinel file.
_IN_DOCKER = os.path.exists("/.dockerenv")
_CHROME_BIN = os.getenv("CHROME_EXECUTABLE_PATH") or None

from .base import (
    Adapter,
    Listing,
    detect_no_wg,
    detect_price_on_request,
    detect_teaser_price,
)

log = logging.getLogger(__name__)

_BASE_SEARCH_URL = (
    "https://www.homegate.ch/rent/real-estate/city-zurich/matching-list"
)

_MAX_PAGES = 10
_STATE_TIMEOUT_S = 30  # seconds to wait for __INITIAL_STATE__ per page


class HomegateAdapter(Adapter):
    name = "homegate"

    def __init__(
        self,
        min_rooms: float,
        max_rent_chf: float,
        session=None,  # kept for interface compatibility; not used
    ) -> None:
        self._min_rooms = min_rooms
        self._max_rent_chf = max_rent_chf

    def search(self) -> list[Listing]:
        try:
            return asyncio.run(self._async_search())
        except Exception as exc:
            log.error("platform=homegate action=search_failed error=%r", str(exc))
            return []

    # ── async implementation ─────────────────────────────────────────────────

    async def _async_search(self) -> list[Listing]:
        extra_args = ["--disable-dev-shm-usage"]
        if _IN_DOCKER:
            extra_args.append("--no-sandbox")
        browser = await uc.start(
            headless=False,
            browser_executable_path=_CHROME_BIN,
            browser_args=extra_args,
        )
        try:
            return await self._fetch_pages(browser)
        finally:
            browser.stop()
            # Let the event loop drain subprocess transports before asyncio.run()
            # closes the loop; avoids "I/O operation on closed pipe" noise on Windows.
            await asyncio.sleep(0.5)

    def _build_url(self, page: int) -> str:
        params = {
            "ep": str(page),
            "ac": str(math.floor(self._min_rooms)),
            "al": str(int(self._max_rent_chf)),
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

    async def _fetch_pages(self, browser) -> list[Listing]:
        listings: list[Listing] = []
        tab = None

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
            for raw in raw_listings:
                try:
                    listing = _parse(raw)
                    if listing:
                        listings.append(listing)
                except Exception:
                    raw_id = (raw.get("listing") or raw).get("id", "?")
                    log.warning(
                        "platform=homegate action=item_parse_error id=%s",
                        raw_id,
                        exc_info=True,
                    )

            log.info(
                "platform=homegate action=page_fetched page=%d/%d listings_so_far=%d",
                page_num, page_count, len(listings),
            )

            if page_num >= page_count:
                break
            await asyncio.sleep(random.uniform(1.0, 2.5))

        log.info("platform=homegate action=fetched count=%d", len(listings))
        return listings


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
