from __future__ import annotations

"""
Comparis.ch adapter — scrapes listings from the Comparis real-estate marketplace.

Background
----------
Comparis aggregates listings from Swiss portals (Flatfox, IS24, Homegate, UrbanHome,
etc.) and adds some exclusive listings.  Its real-estate marketplace is a Next.js app.

Transport
---------
The site is protected by DataDome (``x-datadome: protected``; no Cloudflare).
Plain HTTP, FlareSolverr, and headless Chrome all fail DataDome's JS fingerprinting.
``nodriver`` launches a real Chrome browser via a non-CDP protocol that DataDome
cannot detect.

Search URL
----------
  https://www.comparis.ch/immobilien/result/list
    ?requestobject=<URL-encoded JSON>
    &page=N          (0-indexed; omitted for page 0)

  The ``requestobject`` JSON holds all filter criteria:
    DealType=10          → rent
    LocationSearchString → "zurich"
    RoomsFrom            → minimum room count (string, e.g. "5")
    PriceFrom            → minimum rent CHF (string)
    PriceTo              → maximum rent CHF (string)
    Sort=11              → newest first

  All filters are applied server-side. The SSR response for each page is in
  ``props.pageProps.initialResultData``:
    resultItems     → list of listing objects
    totalPages      → total page count (0-indexed pagination)
    numberOfResults → total filtered result count

  The old path ``/immobilien/marktplatz/suche/mieten?RegionId=...`` is dead (404
  "Ups!"). The new path ``/immobilien/marktplatz/zuerich/wohnung/mieten?p=N``
  ignores all filter params server-side. Only the ``result/list?requestobject=...``
  path provides genuine server-side filtering.

JSON shape
----------
  Key item fields:
    AdId            ← listing identifier
    Title           ← listing title
    PriceValue      ← monthly rent CHF (numeric)
    EssentialInformation  ← ["5.5 Zimmer", "3. OG", …] (rooms in index 0)
    Address         ← ["8006 Zürich"] (postcode + city, no street number)
    Remarks         ← HTML description

  Detail URL: ``/immobilien/marktplatz/details/show/{AdId}``

Consent wall
------------
  Comparis shows a CMP consent dialog on first visit.  The "I Accept" / "Alle
  akzeptieren" button is in the main document (not an iframe); click it after
  ~6 seconds to dismiss.

Docker / Raspberry Pi note
--------------------------
headless=True fails DataDome.  The Docker entrypoint starts Xvfb (:99) so
Chrome can open a headed window with no physical screen.  --no-sandbox is
added automatically when /.dockerenv is detected.
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

_BASE_HOST = "https://www.comparis.ch"
_SEARCH_PATH = "/immobilien/result/list"
_DETAIL_BASE = f"{_BASE_HOST}/immobilien/marktplatz/details/show"

# Base requestobject template — rooms and price are added dynamically.
_BASE_REQUEST_OBJECT: dict = {
    "DealType": 10,
    "SwapProperty": 1,
    "SiteId": 0,
    "RootPropertyTypes": [],
    "PropertyTypes": [],
    "FloorSearchType": 0,
    "LivingSpaceFrom": None,
    "LivingSpaceTo": None,
    "ComparisPointsMin": 0,
    "AdAgeMax": 0,
    "AdAgeInHoursMax": None,
    "Keyword": "",
    "WithImagesOnly": None,
    "WithPointsOnly": None,
    "Radius": None,
    "MinAvailableDate": "1753-01-01T00:00:00",
    "MinChangeDate": "1753-01-01T00:00:00",
    "LocationSearchString": "zurich",
    "Sort": 11,              # newest first
    "ShowComparisPoints": False,
    "HasBalcony": False,
    "HasTerrace": False,
    "HasFireplace": False,
    "HasDishwasher": False,
    "HasWashingMachine": False,
    "HasLift": False,
    "HasParking": False,
    "PetsAllowed": False,
    "MinergieCertified": False,
    "WheelchairAccessible": False,
    "LowerLeftLatitude": None,
    "LowerLeftLongitude": None,
    "UpperRightLatitude": None,
    "UpperRightLongitude": None,
    "MinYearOfConstruction": None,
    "MaxYearOfConstruction": None,
    "MinRenovationYear": None,
    "HasIndoorParking": False,
    "HasOutdoorParking": False,
    "HasDocuments": False,
}

_MAX_PAGES = 10
_STATE_TIMEOUT_S = 50


class ComparisAdapter(Adapter):
    name = "comparis"

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
            log.error("platform=comparis action=search_failed error=%r", str(exc))
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
            await asyncio.sleep(0.5)

    def _build_url(self, page: int) -> str:
        req = dict(_BASE_REQUEST_OBJECT)
        req["RoomsFrom"] = str(int(self._min_rooms))
        req["RoomsTo"] = None
        req["PriceFrom"] = str(int(self._min_rent_chf))
        req["PriceTo"] = str(int(self._max_rent_chf))
        params: dict = {"requestobject": json.dumps(req, separators=(",", ":"))}
        if page > 0:
            params["page"] = str(page)
        return f"{_BASE_HOST}{_SEARCH_PATH}?{urlencode(params)}"

    async def _accept_consent(self, tab) -> None:
        """Click the CMP consent accept button if the dialog is showing."""
        await asyncio.sleep(6)
        try:
            raw = await tab.evaluate(r"""
                JSON.stringify((function() {
                    var terms = [
                        'accept', 'i accept', 'alle akzeptieren', 'alles akzeptieren',
                        'tout accepter', 'zustimmen', 'einverstanden', 'akzeptieren',
                        'agree', 'annehmen', 'alle cookies', 'accept & close'
                    ];

                    function tryClick(root) {
                        if (!root || !root.querySelectorAll) return null;
                        var btn = root.getElementById && root.getElementById('onetrust-accept-btn-handler');
                        if (!btn) btn = root.querySelector && root.querySelector('.onetrust-accept-btn-handler');
                        if (!btn) btn = root.querySelector && root.querySelector('[data-testid="accept-all"]');
                        if (!btn) {
                            var all = Array.from(root.querySelectorAll('button, [role="button"], a[role="button"]'));
                            btn = all.find(function(b) {
                                var t = (b.innerText || b.textContent || '').toLowerCase().trim();
                                return terms.some(function(x) { return t.includes(x); });
                            }) || null;
                        }
                        if (btn) {
                            btn.click();
                            return (btn.innerText || btn.textContent || '').trim().substring(0, 50);
                        }
                        return null;
                    }

                    function searchShadow(root) {
                        var r = tryClick(root);
                        if (r !== null) return r;
                        var all = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];
                        for (var i = 0; i < all.length; i++) {
                            if (all[i].shadowRoot) {
                                var sr = searchShadow(all[i].shadowRoot);
                                if (sr !== null) return sr;
                            }
                        }
                        return null;
                    }

                    var t = searchShadow(document);
                    if (t !== null) return {clicked: true, text: t, source: 'main'};

                    var iframes = Array.from(document.querySelectorAll('iframe'));
                    for (var j = 0; j < iframes.length; j++) {
                        try {
                            var iDoc = iframes[j].contentDocument
                                     || (iframes[j].contentWindow && iframes[j].contentWindow.document);
                            if (!iDoc) continue;
                            var t2 = searchShadow(iDoc);
                            if (t2 !== null) return {clicked: true, text: t2, source: 'iframe:' + j};
                        } catch(e) {}
                    }

                    var allBtns = Array.from(document.querySelectorAll('button')).map(function(b) {
                        return (b.innerText || b.textContent || '').trim().substring(0, 30);
                    }).filter(Boolean).slice(0, 12);
                    return {clicked: false, availableButtons: allBtns};
                })())
            """)
            if raw and raw not in ("null", "undefined"):
                result = json.loads(raw)
                if result.get("clicked"):
                    log.debug(
                        "platform=comparis action=consent_accepted text=%r source=%s",
                        result.get("text"), result.get("source"),
                    )
                    await asyncio.sleep(4)
                else:
                    log.debug(
                        "platform=comparis action=consent_not_found buttons=%s",
                        result.get("availableButtons"),
                    )
        except Exception:
            pass

    async def _wait_for_state(self, tab) -> tuple[list[dict], int] | None:
        """
        Poll until __NEXT_DATA__ contains resultItems for the current page.

        Returns (items, total_pages) or None on timeout.
        """
        deadline = asyncio.get_event_loop().time() + _STATE_TIMEOUT_S
        _dumped = False

        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await tab.evaluate(r"""
                    (function() {
                        var el = document.getElementById('__NEXT_DATA__');
                        if (!el) return JSON.stringify({
                            _debug: true, path: 'no_next_data',
                            title: document.title,
                            bodyStart: document.body ? document.body.innerText.substring(0, 120) : ''
                        });
                        var data;
                        try { data = JSON.parse(el.textContent); } catch(e) { return null; }
                        var ird = data && data.props && data.props.pageProps && data.props.pageProps.initialResultData;
                        if (!ird) {
                            var ppKeys = data && data.props && data.props.pageProps ? Object.keys(data.props.pageProps) : [];
                            return JSON.stringify({_debug: true, path: 'no_ird', ppKeys: ppKeys, title: document.title});
                        }
                        var items = ird.resultItems || [];
                        if (items.length === 0) return JSON.stringify({_debug: true, path: 'empty_items', irdKeys: Object.keys(ird)});
                        return JSON.stringify({
                            items: items,
                            totalPages: ird.totalPages || 1,
                            page: ird.page,
                            numberOfResults: ird.numberOfResults
                        });
                    })()
                """)

                if not raw or raw in ("null", "undefined", ""):
                    await asyncio.sleep(1)
                    continue

                parsed = json.loads(raw)

                if "items" in parsed and not parsed.get("_debug"):
                    items = parsed["items"]
                    page_count = int(parsed.get("totalPages", 1))
                    if items:
                        return items, page_count

                if parsed.get("_debug") and not _dumped:
                    _dumped = True
                    log.debug(
                        "platform=comparis action=state_dump path=%s ppKeys=%s irdKeys=%s title=%r",
                        parsed.get("path"), parsed.get("ppKeys"),
                        parsed.get("irdKeys"), parsed.get("title"),
                    )

            except Exception:
                pass

            await asyncio.sleep(1)

        return None

    async def _fetch_pages(self, browser) -> list[Listing]:
        listings: list[Listing] = []
        tab = None
        _first_item_logged = False

        for page_num in range(_MAX_PAGES):
            url = self._build_url(page_num)
            try:
                if tab is None:
                    tab = await browser.get(url)
                else:
                    await tab.get(url)
            except Exception as exc:
                log.error("platform=comparis action=navigate_error page=%d error=%r", page_num, str(exc))
                break

            if page_num == 0:
                await self._accept_consent(tab)

            result = await self._wait_for_state(tab)
            if result is None:
                log.warning(
                    "platform=comparis action=no_state page=%d — "
                    "DataDome not cleared or __NEXT_DATA__ structure changed; "
                    "re-run with --log-level DEBUG to see pageProps keys.",
                    page_num,
                )
                break

            raw_items, page_count = result

            if not _first_item_logged and raw_items:
                _first_item_logged = True
                sample = raw_items[0]
                log.debug(
                    "platform=comparis action=first_item_sample "
                    "total=%d pages=%d price=%s essential=%s address=%s",
                    page_count * 10,
                    page_count,
                    sample.get("PriceValue"),
                    sample.get("EssentialInformation"),
                    sample.get("Address"),
                )

            for raw in raw_items:
                try:
                    listing = _parse(raw)
                    if listing:
                        listings.append(listing)
                except Exception:
                    raw_id = raw.get("AdId", "?") if isinstance(raw, dict) else "?"
                    log.warning("platform=comparis action=item_parse_error id=%s", raw_id, exc_info=True)

            log.info(
                "platform=comparis action=page_fetched page=%d/%d items=%d listings_so_far=%d",
                page_num, page_count - 1, len(raw_items), len(listings),
            )

            if page_num >= page_count - 1:
                break
            await asyncio.sleep(random.uniform(1.5, 3.0))

        log.info("platform=comparis action=fetched count=%d", len(listings))
        return listings


# ── regex to extract numeric rooms from "5.5 Zimmer" etc. ───────────────────
_ROOMS_RE = re.compile(r"(\d+(?:[.,]\d)?)\s*(?:Zimmer|zimmer|Z\b|Zim)", re.IGNORECASE)


def _parse(item: dict) -> Listing | None:
    ad_id = item.get("AdId")
    if not ad_id:
        return None
    listing_id = str(ad_id)

    title = (item.get("Title") or "").strip()
    remarks_html = item.get("Remarks") or ""
    description = re.sub(r"<[^>]+>", " ", remarks_html).strip() if remarks_html else title
    full_text = f"{title} {description}"

    price_raw = item.get("PriceValue")
    price_chf: float | None = float(price_raw) if price_raw is not None else None
    price_is_teaser = detect_teaser_price(full_text)
    price_on_request = price_chf is None and detect_price_on_request(full_text)

    rooms: float | None = None
    for part in (item.get("EssentialInformation") or []):
        m = _ROOMS_RE.search(str(part))
        if m:
            rooms = float(m.group(1).replace(",", "."))
            break

    addr_parts = item.get("Address") or []
    addr_str = addr_parts[0].strip() if addr_parts else ""
    postcode = ""
    if addr_str:
        m = re.match(r"^(\d{4})\b", addr_str)
        if m:
            postcode = m.group(1)

    url = f"{_DETAIL_BASE}/{listing_id}"

    if not title:
        title = f"{rooms or '?'}R Zürich {postcode}"

    return Listing(
        id=listing_id,
        url=url,
        title=title,
        price_chf=price_chf,
        rooms=rooms,
        postcode=postcode or None,
        address=addr_str or None,
        available_from=None,
        description=description[:500] if description else None,
        platform="comparis",
        price_is_teaser=price_is_teaser,
        price_on_request=price_on_request,
        no_wg_clause=detect_no_wg(full_text),
    )
