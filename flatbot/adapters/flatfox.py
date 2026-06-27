from __future__ import annotations

"""
Flatfox adapter — fetches listings from the public Flatfox JSON API.

Two-step search flow (observed from the website's own XHR calls):
  1. GET /api/v1/pin/ — accepts bbox + filter params, returns a filtered
     array of {pk, latitude, longitude, price_display, ...} objects.
  2. GET /api/v1/public-listing/?pk=X&pk=Y&... — fetches full listing
     details for those specific PKs.

The /api/v1/public-listing/ endpoint alone does NOT support bbox or
rooms/price filtering (confirmed from the OpenAPI spec).  All filters
must go through the pin endpoint.
"""

import logging
import random
import time

import httpx

from .base import (
    Adapter,
    Listing,
    detect_no_wg,
    detect_price_on_request,
    detect_teaser_price,
)

log = logging.getLogger(__name__)

_BASE_URL = "https://flatfox.ch/"
_PIN_URL = "https://flatfox.ch/api/v1/pin/"
_LISTING_URL = "https://flatfox.ch/api/v1/public-listing/"
# Bounding box for city of Zurich (excludes most of the canton)
_ZURICH_BBOX = {"east": 8.624, "west": 8.441, "north": 47.434, "south": 47.310}
_MAX_PIN_COUNT = 400  # max results the pin endpoint returns
_BATCH_SIZE = 48      # PKs per public-listing request (matches website behaviour)


class FlatfoxAdapter(Adapter):
    name = "flatfox"

    def __init__(
        self,
        min_rooms: float,
        max_rent_chf: float,
    ) -> None:
        self._min_rooms = min_rooms
        self._max_rent_chf = max_rent_chf
        self._client = httpx.Client(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
            },
            timeout=30,
            follow_redirects=True,
        )

    def search(self) -> list[Listing]:
        pks = self._fetch_pks()
        if not pks:
            log.info("platform=flatfox action=no_pins_returned")
            return []

        listings = self._fetch_listings(pks)
        log.info("platform=flatfox action=fetched count=%d", len(listings))
        return listings

    # ── private helpers ──────────────────────────────────────────────────────

    def _fetch_pks(self) -> list[int]:
        params = {
            "east": str(_ZURICH_BBOX["east"]),
            "west": str(_ZURICH_BBOX["west"]),
            "north": str(_ZURICH_BBOX["north"]),
            "south": str(_ZURICH_BBOX["south"]),
            "min_rooms": str(self._min_rooms),
            "max_price": str(int(self._max_rent_chf)),
            "max_count": str(_MAX_PIN_COUNT),
        }
        try:
            resp = self._client.get(_PIN_URL, params=params, headers={"Accept": "application/json"})
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.error("platform=flatfox action=pin_request_error error=%r", str(exc))
            return []

        if not isinstance(data, list):
            log.error("platform=flatfox action=pin_unexpected_response type=%s", type(data))
            return []

        pks = [item["pk"] for item in data if isinstance(item, dict) and "pk" in item]
        log.info("platform=flatfox action=pins_fetched count=%d", len(pks))
        return pks

    def _fetch_listings(self, pks: list[int]) -> list[Listing]:
        listings: list[Listing] = []

        for batch_start in range(0, len(pks), _BATCH_SIZE):
            batch = pks[batch_start : batch_start + _BATCH_SIZE]
            params: list[tuple[str, str]] = [("pk", str(pk)) for pk in batch]
            params.append(("limit", str(len(batch))))
            params.append(("ordering", "-pk"))

            try:
                resp = self._client.get(_LISTING_URL, params=params, headers={"Accept": "application/json"})
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.error(
                    "platform=flatfox action=listing_request_error batch=%d error=%r",
                    batch_start, str(exc),
                )
                break

            results = data.get("results", []) if isinstance(data, dict) else []
            for item in results:
                try:
                    listing = _parse(item)
                    if listing:
                        listings.append(listing)
                except Exception:
                    log.warning(
                        "platform=flatfox action=item_parse_error id=%s",
                        item.get("pk", "?"),
                        exc_info=True,
                    )

            if batch_start + _BATCH_SIZE < len(pks):
                time.sleep(random.uniform(1.0, 2.5))

        return listings


def _parse(item: dict) -> Listing | None:
    pk = item.get("pk")
    if pk is None:
        return None

    raw_url = item.get("url") or ""
    if raw_url.startswith("/"):
        raw_url = f"https://flatfox.ch{raw_url}"
    url = raw_url or f"https://flatfox.ch/en/flat/{pk}/"

    title = (
        item.get("public_title")
        or item.get("short_title")
        or item.get("description_title")
        or item.get("title")
        or ""
    )
    description = item.get("description") or ""
    full_text = f"{title} {description}"

    # rent_gross is null for some listings; fall back to rent_net (net rent without utilities)
    _price_raw = item.get("rent_gross") if item.get("rent_gross") is not None else item.get("rent_net")
    try:
        price_chf = float(_price_raw) if _price_raw is not None else None
    except (ValueError, TypeError):
        price_chf = None

    price_display_type = str(item.get("price_display_type") or "").lower()
    price_is_teaser = "from" in price_display_type or "ab" in price_display_type
    if not price_is_teaser:
        price_is_teaser = detect_teaser_price(full_text)

    price_on_request = price_chf is None and detect_price_on_request(full_text)

    rooms_raw = item.get("number_of_rooms")
    try:
        rooms = float(rooms_raw) if rooms_raw is not None else None
    except (ValueError, TypeError):
        rooms = None

    postcode = str(item.get("zipcode") or "").strip()
    city = item.get("city") or ""
    street = item.get("street") or ""
    address_parts = [p for p in [street, f"{postcode} {city}".strip()] if p]
    address = ", ".join(address_parts) or None

    available_from = item.get("moving_date") or item.get("available_from")

    if not title:
        title = f"{rooms or '?'}R Zürich {postcode}"

    return Listing(
        id=str(pk),
        url=url,
        title=title,
        price_chf=price_chf,
        rooms=rooms,
        postcode=postcode or None,
        address=address,
        available_from=str(available_from) if available_from else None,
        description=description,
        platform="flatfox",
        price_is_teaser=price_is_teaser,
        price_on_request=price_on_request,
        no_wg_clause=detect_no_wg(full_text),
    )
