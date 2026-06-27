from __future__ import annotations

import logging
from datetime import datetime, timezone

from .adapters.base import Listing

log = logging.getLogger(__name__)

_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

_HEADERS = [
    "Seen At",
    "Platform",
    "ID",
    "Title",
    "URL",
    "Rooms",
    "Rent (CHF)",
    "Postcode",
    "Address",
    "Available From",
    "Flags",
    "Human Sent Message",
]


def _worksheet(sheets_id: str, service_account_json: str):
    import gspread

    gc = gspread.service_account(filename=service_account_json)
    return gc.open_by_key(sheets_id).sheet1


def ensure_headers(sheets_id: str, service_account_json: str) -> None:
    if not sheets_id or not service_account_json:
        return
    try:
        ws = _worksheet(sheets_id, service_account_json)
        if not ws.row_values(1):
            ws.append_row(_HEADERS, value_input_option="RAW")
            log.info("action=sheets_headers_written")
    except Exception:
        log.warning("action=sheets_headers_failed", exc_info=True)


def append_row(listing: Listing, sheets_id: str, service_account_json: str) -> None:
    if not sheets_id or not service_account_json:
        return

    flags: list[str] = []
    if listing.price_is_teaser:
        flags.append("teaser-price")
    if listing.price_on_request:
        flags.append("price-on-request")
    if listing.no_wg_clause:
        flags.append("no-wg-clause")

    row = [
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        listing.platform,
        listing.id,
        listing.title,
        listing.url,
        str(listing.rooms or ""),
        str(listing.price_chf or ""),
        listing.postcode or "",
        listing.address or "",
        listing.available_from or "",
        ", ".join(flags),
        "",  # Human Sent Message — filled manually
    ]

    try:
        ws = _worksheet(sheets_id, service_account_json)
        ws.append_row(row, value_input_option="RAW")
        log.info(
            "platform=%s action=sheets_appended id=%s",
            listing.platform,
            listing.id,
        )
    except Exception:
        log.warning(
            "platform=%s action=sheets_failed id=%s",
            listing.platform,
            listing.id,
            exc_info=True,
        )
