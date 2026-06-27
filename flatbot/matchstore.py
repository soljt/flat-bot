from __future__ import annotations

"""
MatchStore — cross-platform deduplication store.

Maintains an append-only JSONL file that maps a normalized match key
(derived from street address, or a postcode+rooms+price bucket when no
address is available) to the first Listing seen at that location.

When a later adapter returns a listing whose key is already in the store,
the pipeline skips the email and instead appends the duplicate URL to the
Google Sheet row already created for the original notification.
"""

import json
import logging
import os
import re

from .adapters.base import Listing

log = logging.getLogger(__name__)


def _normalize_address(address: str) -> str:
    """Lowercase, strip all punctuation/special chars, collapse whitespace."""
    s = address.lower()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


class MatchRecord:
    __slots__ = ("uid", "url", "sheet_row")

    def __init__(self, uid: str, url: str, sheet_row: int | None) -> None:
        self.uid = uid
        self.url = url
        self.sheet_row = sheet_row


class MatchStore:
    """
    Cross-platform deduplication store (JSONL, append-only, crash-safe).

    One instance is shared across the full run_cycle call so that a listing
    found on Flatfox is remembered when Homegate returns the same apartment
    in the same cycle.

    Key derivation (``match_key``):
    - Street address present → ``"addr:<normalised address string>"``
    - No address but postcode+rooms+price known → ``"bucket:<postcode>|<rooms>|<price_bucket>"``
      where price_bucket = round(price_chf / 50) (CHF 50 granularity)
    - Neither → ``None``  (listing is not de-dupable; always notified)
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._records: dict[str, MatchRecord] = {}
        self._load()

    # ── private ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not os.path.exists(self._path):
            log.info("action=matchstore_init path=%s", self._path)
            return
        with open(self._path) as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    key = entry["key"]
                    self._records[key] = MatchRecord(
                        uid=entry["uid"],
                        url=entry["url"],
                        sheet_row=entry.get("sheet_row"),
                    )
                except (KeyError, json.JSONDecodeError):
                    log.warning("action=matchstore_bad_line line=%r", line[:80])
        log.info(
            "action=matchstore_loaded count=%d path=%s",
            len(self._records),
            self._path,
        )

    # ── public API ────────────────────────────────────────────────────────────

    def match_key(self, listing: Listing) -> str | None:
        """Return a platform-independent dedup key, or None if not possible."""
        if listing.address:
            return "addr:" + _normalize_address(listing.address)
        if (
            listing.postcode
            and listing.rooms is not None
            and listing.price_chf is not None
        ):
            bucket = round(listing.price_chf / 50)
            return f"bucket:{listing.postcode}|{listing.rooms}|{bucket}"
        return None

    def lookup(self, key: str) -> MatchRecord | None:
        """Return the stored record for *key*, or None."""
        return self._records.get(key)

    def add(
        self,
        key: str,
        uid: str,
        url: str,
        sheet_row: int | None = None,
    ) -> None:
        """Persist a new match record (in memory + appended to JSONL file)."""
        record = MatchRecord(uid=uid, url=url, sheet_row=sheet_row)
        self._records[key] = record
        entry = {"key": key, "uid": uid, "url": url, "sheet_row": sheet_row}
        with open(self._path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        log.info("action=matchstore_added key=%s uid=%s", key, uid)
