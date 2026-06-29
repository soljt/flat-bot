from __future__ import annotations

"""
NewHome.ch adapter — fetches listings via in-page XHR using nodriver.

Background
----------
newhome.ch is a Swiss property portal operated by Swiss Post.  Its Angular
frontend calls a JSON API at ``service.newhome.ch``.  The HTML layer is
protected by a Cloudflare Managed Challenge only — no DataDome.

Transport
---------
Direct HTTP calls (httpx) and FlareSolverr + httpx cookie injection both fail
because Cloudflare enforces ``Sec-Fetch-Site: same-site`` for the
``service.newhome.ch`` API — it only allows requests originating from within
the ``www.newhome.ch`` browser context.

Solution: use ``nodriver`` to load the Angular search-results page (CF
challenge solved automatically by headed Chrome), then call the service API
from within the page using ``page.evaluate("fetch(...)")`` — the in-page XHR
carries the right ``Sec-Fetch-Site`` header and the CF cookies already in the
browser's cookie jar, so the request succeeds.

Search page
-----------
  https://www.newhome.ch/de/mieten/suchen/wohnung/ort-zuerich/liste

Search API called from within the page
---------------------------------------
  GET https://service.newhome.ch/api/api/SearchListingRequest
    ?location=1;2560   — Zürich municipality (covers all 80xx postcodes)
    &offerType=2       — rent
    &propertyType=100  — house or apartment (API rejects propertyType=0)
    &roomsMin=N        — server-side min room filter
    &roomsMax=99       — no effective upper cap
    &priceMin=N        — server-side min rent CHF
    &priceMax=N        — server-side max rent CHF
    &rowCount=20
    &skipCount=N
    &order=0
    &numberOfSpecialPromotions=0
    &languageIso=de

  Response: { entries[], totalResultCount }

Entry fields
------------
  immocode (ID), title, street (incl. house number), city, postalCode,
  price (gross CHF), rooms, availabilityDate

Detail URL
----------
  https://www.newhome.ch/de/mieten/immobilien/detail/{immocode}

Docker / Raspberry Pi note
--------------------------
headless=True *might* fail CF (same CF fingerprinting concern as DataDome
sites).  Use headed Chrome via Xvfb, same as Homegate and ImmoScout24.
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

from .base import Adapter, Listing, detect_no_wg, detect_price_on_request, detect_teaser_price

log = logging.getLogger(__name__)

_SEARCH_PAGE = "https://www.newhome.ch/de/mieten/suchen/wohnung/ort-zuerich/liste"
_SEARCH_API = "https://service.newhome.ch/api/api/SearchListingRequest"
_DETAIL_BASE = "https://www.newhome.ch/en/renting/properties/apartment/apartment"

_ZURICH_LOCATION = "1;2560"
_ROW_COUNT = 20
_MAX_PAGES = 15

# Seconds to wait for the Angular app to initialize before making XHR calls.
_LOAD_WAIT_S = 12
# Timeout for each individual fetch() call (ms).
_FETCH_TIMEOUT_MS = 30_000


class NewHomeAdapter(Adapter):
    name = "newhome"

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
            log.error("platform=newhome action=search_failed error=%r", str(exc))
            return []

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

    def _build_api_url(self, skip: int) -> str:
        # Build manually — urlencode encodes ';' in the location param which
        # the NewHome API does not accept (expects the raw "1;2560" format).
        # propertyType=100 = house or apartment; API rejects propertyType=0 (None).
        # roomsMin/priceMin/priceMax are applied server-side.
        return (
            f"{_SEARCH_API}"
            f"?location={_ZURICH_LOCATION}"
            f"&offerType=2"
            f"&propertyType=100"
            f"&roomsMin={int(self._min_rooms)}"
            f"&roomsMax=99"
            f"&priceMin={int(self._min_rent_chf)}"
            f"&priceMax={int(self._max_rent_chf)}"
            f"&rowCount={_ROW_COUNT}"
            f"&skipCount={skip}"
            f"&order=0"
            f"&numberOfSpecialPromotions=0"
            f"&languageIso=de"
        )

    async def _fetch_pages(self, browser) -> list[Listing]:
        listings: list[Listing] = []
        tab = None
        _first_entry_logged = False

        # Navigate to the Angular search page; nodriver clears CF automatically.
        try:
            tab = await browser.get(_SEARCH_PAGE)
        except Exception as exc:
            log.error("platform=newhome action=navigate_error error=%r", str(exc))
            return []

        # Wait for Angular to initialise and establish its CF session.
        log.info("platform=newhome action=waiting_for_angular")
        await asyncio.sleep(_LOAD_WAIT_S)

        skip = 0
        total: int | None = None

        for page_num in range(1, _MAX_PAGES + 1):
            api_url = self._build_api_url(skip)

            # Call the API from within the page context so the XHR carries
            # the correct Sec-Fetch-Site and CF cookies already in the browser.
            # nodriver's evaluate() doesn't await Promises, so we store the
            # result on window and poll until it appears.
            result_key = f"_nh_result_{page_num}"
            try:
                await tab.evaluate(f"""
                    window[{json.dumps(result_key)}] = null;
                    fetch({json.dumps(api_url)}, {{
                        credentials: 'include',
                        headers: {{'Accept': 'application/json', 'ngsw-bypass': 'true'}}
                    }}).then(function(r) {{
                        return r.text().then(function(body) {{
                            if (r.ok) {{
                                try {{ return {{ok: true, data: JSON.parse(body)}}; }}
                                catch(e) {{ return {{ok: false, status: r.status, body: body.substring(0,500)}}; }}
                            }}
                            return {{ok: false, status: r.status, body: body.substring(0,500)}};
                        }});
                    }}).then(function(d) {{
                        window[{json.dumps(result_key)}] = JSON.stringify(d);
                    }}).catch(function(e) {{
                        window[{json.dumps(result_key)}] = JSON.stringify({{error: String(e)}});
                    }});
                """)
            except Exception as exc:
                log.error("platform=newhome action=fetch_start_error page=%d error=%r", page_num, str(exc))
                break

            raw = None
            for _ in range(30):
                await asyncio.sleep(1)
                try:
                    raw = await tab.evaluate(f"window[{json.dumps(result_key)}]")
                    if raw and raw not in ("null", "undefined", ""):
                        break
                except Exception:
                    pass

            if not raw or raw in ("null", "undefined", ""):
                log.error("platform=newhome action=fetch_timeout page=%d", page_num)
                break

            try:
                data = json.loads(raw)
            except Exception as exc:
                log.error(
                    "platform=newhome action=parse_error page=%d raw=%r error=%r",
                    page_num, raw[:200], str(exc),
                )
                break

            if "error" in data:
                log.error("platform=newhome action=api_error page=%d error=%r", page_num, data["error"])
                break

            if not data.get("ok"):
                log.error(
                    "platform=newhome action=api_error page=%d status=%s body=%r",
                    page_num, data.get("status"), data.get("body"),
                )
                break

            data = data.get("data", data)

            if total is None:
                total = int(data.get("totalResultCount") or 0)
                log.info("platform=newhome action=start total_count=%d", total)

            entries = data.get("entries") or []

            if not _first_entry_logged and entries:
                _first_entry_logged = True
                e0 = entries[0]
                log.debug(
                    "platform=newhome action=first_entry_sample keys=%s "
                    "price=%s rooms=%s postalCode=%s",
                    list(e0.keys()), e0.get("price"), e0.get("rooms"), e0.get("postalCode"),
                )

            for entry in entries:
                try:
                    listing = _parse(entry)
                    if listing:
                        listings.append(listing)
                except Exception:
                    log.warning(
                        "platform=newhome action=item_parse_error id=%s",
                        entry.get("immocode", "?"),
                        exc_info=True,
                    )

            log.info(
                "platform=newhome action=page_fetched page=%d skip=%d "
                "entries=%d listings_so_far=%d",
                page_num, skip, len(entries), len(listings),
            )

            skip += _ROW_COUNT
            if not entries or (total is not None and skip >= total):
                break

            await asyncio.sleep(random.uniform(1.0, 2.5))

        log.info("platform=newhome action=fetched count=%d", len(listings))
        return listings


def _city_slug(city: str) -> str:
    """Convert a city name to NewHome's city-<slug> URL segment."""
    slug = city.lower().replace("ü", "ue").replace("ö", "oe").replace("ä", "ae").replace(" ", "-")
    return f"city-{slug}"


def _rooms_slug(rooms: float | None) -> str:
    """Format rooms count as the NewHome URL segment, e.g. 6.5-room."""
    if rooms is None:
        return "5-room"
    r = int(rooms) if rooms == int(rooms) else rooms
    return f"{r}-room"


def _parse(entry: dict) -> Listing | None:
    immocode = entry.get("immocode")
    if not immocode:
        return None
    listing_id = str(immocode)

    title = (entry.get("title") or "").strip()
    description = title
    full_text = title

    price_raw = entry.get("price")
    price_chf: float | None = float(price_raw) if price_raw is not None else None
    price_is_teaser = detect_teaser_price(full_text)
    price_on_request = price_chf is None and detect_price_on_request(full_text)

    rooms_raw = entry.get("rooms")
    rooms: float | None = float(rooms_raw) if rooms_raw is not None else None

    street = (entry.get("street") or "").strip()
    postcode = str(entry.get("postalCode") or "").strip()
    city = (entry.get("city") or "").strip()
    address_parts = [p for p in [street, f"{postcode} {city}".strip()] if p]
    address = ", ".join(address_parts) or None

    avail_raw = str(entry.get("availabilityDate") or "")[:10]
    available_from: str | None = avail_raw if re.match(r"^\d{4}-\d{2}-\d{2}$", avail_raw) else None

    city_slug = _city_slug(entry.get("city") or "Zürich")
    rooms_slug = _rooms_slug(rooms)
    url = f"{_DETAIL_BASE}/{city_slug}/{rooms_slug}/detail/{listing_id}"
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
        available_from=available_from,
        description=description,
        platform="newhome",
        price_is_teaser=price_is_teaser,
        price_on_request=price_on_request,
        no_wg_clause=detect_no_wg(full_text),
    )
