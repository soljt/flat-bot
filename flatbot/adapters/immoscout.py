from __future__ import annotations

"""
ImmoScout24.ch adapter — scrapes listings from the ImmoScout24 search page.

Background
----------
ImmoScout24.ch is one of the two largest Swiss real-estate portals.  Its search
API is not publicly accessible; the search page is a Next.js SPA that embeds
server-rendered listing data in a ``<script id="__NEXT_DATA__">`` tag.

Transport
---------
The site is protected by both Cloudflare Managed Challenge and DataDome (same
stack as Homegate).  Plain HTTP, FlareSolverr, and headless Chrome all fail
DataDome's JS fingerprinting.  ``nodriver`` launches a real Chrome browser via
a non-CDP protocol that DataDome cannot detect, and passes all challenges
automatically in headed (visible window) mode.

Search URL
----------
  https://www.immoscout24.ch/en/real-estate/rent/city-zurich
    ?nrf=<min_rooms>   — number of rooms from (minimum)
    &pf=<min_price>    — price from (minimum gross rent CHF)
    &pt=<max_price>    — price to (maximum gross rent CHF)
    &pn=<page>         — page number (1-indexed)

JSON shape (__NEXT_DATA__)
--------------------------
Extracted from the ``<script id="__NEXT_DATA__">`` element on each page.

  props.pageProps.searchResult.listings   — array of listing objects
  props.pageProps.searchResult.pagination.totalPages  — total page count

Each listing is shaped roughly like:

  {
    "id": "...",
    "title": "...",
    "description": "...",
    "characteristics": { "numberOfRooms": 5.5 },
    "prices": { "rent": { "gross": 4800, "isFrom": false } },
    "address": { "street": "...", "houseNumber": "...",
                 "postalCode": "8001", "city": "Zürich" },
    "availableFrom": "2025-09-01"
  }

If ImmoScout24 restructures their JSON, run with --log-level DEBUG and look for
the ``action=state_dump`` log line, which prints the top-level pageProps keys,
and the ``action=first_listing_sample`` line, which shows the first raw listing.
Adjust _LISTINGS_PATHS / _PAGE_COUNT_PATHS and _parse accordingly.

Docker / Raspberry Pi note
--------------------------
headless=True fails DataDome.  The Docker entrypoint starts Xvfb (:99) so
Chrome can open a headed window with no physical screen.  --no-sandbox is
appended automatically when /.dockerenv is detected.
"""

import asyncio
import json
import logging
import os
import random
import re
from urllib.parse import urlencode

import nodriver as uc

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

_BASE_URL = "https://www.immoscout24.ch/en/real-estate/rent/city-zurich"
_BASE_HOST = "https://www.immoscout24.ch"

_MAX_PAGES = 10
_STATE_TIMEOUT_S = 45  # slightly longer than Homegate; IS24 can be slower to hydrate


class ImmoScout24Adapter(Adapter):
    name = "immoscout24"

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

    def search(self) -> list[Listing]:
        try:
            return asyncio.run(self._async_search())
        except Exception as exc:
            log.error("platform=immoscout24 action=search_failed error=%r", str(exc))
            return []

    def get_available_from(self, url: str) -> str | None:
        try:
            return asyncio.run(self._async_get_available_from(url))
        except Exception as exc:
            log.warning("platform=immoscout24 action=detail_fetch_failed url=%s error=%r", url, str(exc))
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
            await asyncio.sleep(0.5)

    def _build_url(self, page: int) -> str:
        params = {
            "nrf": str(int(self._min_rooms)),   # number of rooms from (min)
            "pf": str(int(self._min_rent_chf)), # price from (min rent CHF)
            "pt": str(int(self._max_rent_chf)), # price to (max rent CHF)
            "pn": str(page),                    # page number (1-indexed)
        }
        return f"{_BASE_URL}?{urlencode(params)}"

    async def _wait_for_state(self, tab) -> tuple[list[dict], int] | None:
        """
        Poll until listing data is available on the page.

        IS24 does not embed data in __NEXT_DATA__ or common SSR globals; it
        fetches listings via XHR after initial render.  We poll both for SSR
        globals (in case the framework changes) and for listing card elements
        in the DOM (populated after the XHR completes).

        Returns (listings, page_count) on success or None on timeout.
        """
        deadline = asyncio.get_event_loop().time() + _STATE_TIMEOUT_S
        _dumped = False

        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await tab.evaluate(r"""
                    (function() {
                        // ── Path 1: __NEXT_DATA__ (Next.js Pages Router) ──────
                        var ndEl = document.getElementById('__NEXT_DATA__');
                        if (ndEl) {
                            try {
                                var nd = JSON.parse(ndEl.textContent);
                                var pp = nd && nd.props && nd.props.pageProps;
                                if (pp) {
                                    var sr = pp.searchResult || pp.results;
                                    var items = sr && (sr.listings || sr.items || sr.results);
                                    if (Array.isArray(items) && items.length > 0) {
                                        var pc = (sr.pagination && (sr.pagination.totalPages || sr.pagination.pageCount)) || 1;
                                        return JSON.stringify({ listings: items, pageCount: pc });
                                    }
                                }
                            } catch(e) {}
                        }

                        // ── Path 2: window.__INITIAL_STATE__ (Nuxt/Redux) ─────
                        if (window.__INITIAL_STATE__) {
                            var s = window.__INITIAL_STATE__;
                            var nested = (s.resultList && s.resultList.search && s.resultList.search.fullSearch && s.resultList.search.fullSearch.result);
                            if (nested && Array.isArray(nested.listings) && nested.listings.length > 0) {
                                return JSON.stringify({ listings: nested.listings, pageCount: nested.pageCount || 1 });
                            }
                        }

                        // ── Path 3: DOM card scraping ─────────────────────────
                        // IS24 renders listing cards as article or li elements;
                        // try several selector patterns to find them.
                        var selectors = [
                            '[data-testid="listing-item"]',
                            '[data-testid^="result"]',
                            'article[id^="listing"]',
                            'article[class*="HgCard"]',
                            '[class*="HgCard"][class*="listing"]',
                            'li[class*="HgSrpResultListItem"]',
                            '[class*="ResultListItem"]',
                            '[class*="SearchResult"] article',
                        ];
                        var cards = null;
                        for (var i = 0; i < selectors.length; i++) {
                            var found = document.querySelectorAll(selectors[i]);
                            if (found.length > 0) { cards = found; break; }
                        }

                        if (cards && cards.length > 0) {
                            var listings = Array.from(cards).map(function(card) {
                                // Extract structured data embedded in the card's data attributes or JSON-LD
                                var raw = card.getAttribute('data-listing') || card.getAttribute('data-item') || '';
                                var parsed = {};
                                if (raw) { try { parsed = JSON.parse(raw); } catch(e) {} }

                                // Try JSON-LD inside the card
                                var ld = card.querySelector('script[type="application/ld+json"]');
                                if (ld) { try { var j = JSON.parse(ld.textContent); Object.assign(parsed, j); } catch(e) {} }

                                // DOM-level fallback: read visible text nodes
                                var link = card.querySelector('a[href]');
                                parsed._domUrl = link ? link.href : '';
                                parsed._domText = card.innerText ? card.innerText.substring(0, 300) : '';
                                parsed._selectorUsed = selectors[i] || 'unknown';
                                return parsed;
                            });
                            // Estimate page count from pagination if visible
                            var pgEl = document.querySelector('[aria-label="Last page"], [data-testid="last-page-btn"], .pagination__last, [class*="Pagination"] a:last-child');
                            var pageCount = pgEl ? (parseInt(pgEl.textContent) || 1) : 1;
                            return JSON.stringify({ listings: listings, pageCount: pageCount, _fromDom: true });
                        }

                        // ── Debug: nothing found yet ──────────────────────────
                        // Log counts of candidate elements so we can refine selectors.
                        var dbg = {
                            _debug: true,
                            title: document.title,
                            hasNextData: !!ndEl,
                            hasInitialState: !!window.__INITIAL_STATE__,
                            articleCount: document.querySelectorAll('article').length,
                            liCount: document.querySelectorAll('li[class]').length,
                            windowStateKeys: Object.keys(window).filter(function(k){ return /STATE|DATA|STORE|INITIAL|APP|NUXT|LISTING/i.test(k); }),
                            scriptIds: Array.from(document.querySelectorAll('script[id]')).map(function(s){ return s.id; }),
                        };
                        return JSON.stringify(dbg);
                    })()
                """)

                if not raw or raw in ("null", "undefined", ""):
                    await asyncio.sleep(1)
                    continue

                parsed = json.loads(raw)

                if "listings" in parsed and not parsed.get("_debug"):
                    items = parsed["listings"]
                    page_count = int(parsed.get("pageCount", 1))
                    if items:
                        if parsed.get("_fromDom"):
                            log.debug("platform=immoscout24 action=dom_extraction items=%d", len(items))
                        return items, page_count

                if parsed.get("_debug") and not _dumped:
                    _dumped = True
                    log.debug(
                        "platform=immoscout24 action=state_dump "
                        "title=%r articles=%s lis=%s window_keys=%s script_ids=%s",
                        parsed.get("title"),
                        parsed.get("articleCount"),
                        parsed.get("liCount"),
                        parsed.get("windowStateKeys"),
                        parsed.get("scriptIds"),
                    )

            except Exception:
                pass

            await asyncio.sleep(1)

        return None

    async def _fetch_pages(self, browser) -> list[Listing]:
        listings: list[Listing] = []
        tab = None
        _first_listing_logged = False

        for page_num in range(1, _MAX_PAGES + 1):
            url = self._build_url(page_num)
            try:
                if tab is None:
                    tab = await browser.get(url)
                else:
                    await tab.get(url)
            except Exception as exc:
                log.error(
                    "platform=immoscout24 action=navigate_error page=%d error=%r",
                    page_num, str(exc),
                )
                break

            # Wait for client-side data to load; IS24 fetches listings via XHR.
            await asyncio.sleep(8)

            result = await self._wait_for_state(tab)
            if result is None:
                log.warning(
                    "platform=immoscout24 action=no_initial_state page=%d — "
                    "DataDome challenge not cleared, or __NEXT_DATA__ structure changed. "
                    "Re-run with --log-level DEBUG to see pageProps keys.",
                    page_num,
                )
                break

            raw_listings, page_count = result

            # Log the first listing's raw shape once so the developer can verify
            # field paths during a dry run.
            if not _first_listing_logged and raw_listings:
                _first_listing_logged = True
                sample = raw_listings[0]
                log.debug(
                    "platform=immoscout24 action=first_listing_sample "
                    "keys=%s prices=%s chars=%s addr=%s",
                    list(sample.keys()),
                    sample.get("prices"),
                    sample.get("characteristics"),
                    sample.get("address"),
                )

            for raw in raw_listings:
                try:
                    listing = _parse(raw)
                    if listing:
                        listings.append(listing)
                except Exception:
                    raw_id = raw.get("id", "?") if isinstance(raw, dict) else "?"
                    log.warning(
                        "platform=immoscout24 action=item_parse_error id=%s",
                        raw_id,
                        exc_info=True,
                    )

            log.info(
                "platform=immoscout24 action=page_fetched page=%d/%d listings_so_far=%d",
                page_num, page_count, len(listings),
            )

            if page_num >= page_count:
                break
            await asyncio.sleep(random.uniform(1.0, 2.5))

        log.info("platform=immoscout24 action=fetched count=%d", len(listings))
        return listings


def _extract_available_from_html(html: str) -> str | None:
    """Extract availability from the rendered IS24 detail page.

    Primary source: the structured key-value pair rendered in the page body:
      <dt>Verfügbarkeit:</dt><dd>Nach Vereinbarung</dd>
      <dt>Verfügbarkeit:</dt><dd>Sofort</dd>
      <dt>Verfügbarkeit:</dt><dd>01.09.2026</dd>

    Returns the value verbatim. Falls back to a JSON-embedded ISO date.
    """
    m = re.search(r"<dt>Verf[^<]{0,20}:</dt>\s*<dd>([^<]+)</dd>", html, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        return val or None
    m = re.search(r'"availableFrom"\s*:\s*"(\d{4}-\d{2}-\d{2})', html)
    return m.group(1) if m else None


def _parse(item: dict) -> Listing | None:
    # IS24 sometimes nests the actual listing under a "listing" key
    d = item.get("listing", item)
    if not isinstance(d, dict):
        return None

    listing_id = str(d.get("id") or "").strip()
    if not listing_id:
        return None

    # ── Text ────────────────────────────────────────────────────────────────
    # IS24 may localise text; try "de" first, then fall back to direct fields.
    loc = d.get("localization", {}) or {}
    lang = loc.get("de") or loc.get("fr") or loc.get("en") or {}
    if isinstance(lang, dict):
        text_block = lang.get("text", {}) if isinstance(lang.get("text"), dict) else {}
        title = (
            text_block.get("title")
            or lang.get("title")
            or d.get("title")
            or d.get("name")
            or ""
        )
        description = (
            text_block.get("description")
            or lang.get("description")
            or d.get("description")
            or d.get("teaser")
            or ""
        )
    else:
        title = d.get("title") or d.get("name") or ""
        description = d.get("description") or d.get("teaser") or ""

    full_text = f"{title} {description}"

    # ── Price ────────────────────────────────────────────────────────────────
    prices = d.get("prices", {}) or {}
    rent = prices.get("rent", {}) or {}
    gross = (
        rent.get("gross")
        or rent.get("totalRent")
        or prices.get("gross")
        or d.get("rent")
        or d.get("price")
    )
    price_chf: float | None = float(gross) if gross is not None else None
    price_is_teaser = bool(rent.get("isFrom") or prices.get("isFrom")) or detect_teaser_price(full_text)
    price_on_request = price_chf is None and detect_price_on_request(full_text)

    # ── Rooms ────────────────────────────────────────────────────────────────
    chars = d.get("characteristics", {}) or {}
    rooms_raw = (
        chars.get("numberOfRooms")
        or chars.get("rooms")
        or d.get("numberOfRooms")
        or d.get("rooms")
    )
    rooms: float | None = float(rooms_raw) if rooms_raw is not None else None

    # ── Address ─────────────────────────────────────────────────────────────
    addr = d.get("address", {}) or {}
    street = addr.get("street") or addr.get("streetName") or ""
    house_no = addr.get("houseNumber") or addr.get("streetNumber") or ""
    postcode = str(addr.get("postalCode") or addr.get("zip") or "").strip()
    city = addr.get("city") or addr.get("locality") or addr.get("localityName") or ""
    street_full = f"{street} {house_no}".strip()
    address_parts = [p for p in [street_full, f"{postcode} {city}".strip()] if p]
    address = ", ".join(address_parts) or None

    # ── Available from ───────────────────────────────────────────────────────
    available_from = d.get("availableFrom") or d.get("moveInDate")

    # ── URL ──────────────────────────────────────────────────────────────────
    # Prefer a canonical URL embedded in the listing data; fall back to
    # https://www.immoscout24.ch/mieten/<id> (the language-independent
    # redirect format that IS24 actually supports).
    slug = d.get("url") or d.get("permalink") or d.get("slug") or ""
    if slug:
        if slug.startswith("http"):
            url = slug
        elif slug.startswith("/"):
            url = f"{_BASE_HOST}{slug}"
        else:
            url = f"{_BASE_HOST}/{slug}"
    else:
        url = f"{_BASE_HOST}/mieten/{listing_id}"

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
        platform="immoscout24",
        price_is_teaser=price_is_teaser,
        price_on_request=price_on_request,
        no_wg_clause=detect_no_wg(full_text),
    )
