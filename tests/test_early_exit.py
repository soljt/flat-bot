"""
Unit tests for the seen-aware early-exit pagination (feature A).

- ``page_all_seen`` is the shared stop-signal used by every adapter.
- Flatfox is the only adapter whose pagination is browser-free (plain httpx),
  so it's the one we can drive end-to-end offline. It exercises the full
  "sort PKs newest-first → stop once a whole batch is already seen" path.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import flatbot.adapters.flatfox as flatfox
from flatbot.adapters.base import Listing, page_all_seen
from flatbot.adapters.flatfox import FlatfoxAdapter


def _listing(pid: str) -> Listing:
    return Listing(
        id=pid, url="u", title="t", price_chf=3000.0, rooms=3.0,
        postcode="8001", address="a", available_from=None,
        description="", platform="flatfox",
    )


# ── page_all_seen ────────────────────────────────────────────────────────────

class TestPageAllSeen:
    def test_all_seen_true(self):
        seen = {"flatfox:1", "flatfox:2"}
        assert page_all_seen([_listing("1"), _listing("2")], seen.__contains__)

    def test_one_new_false(self):
        seen = {"flatfox:1"}
        assert not page_all_seen([_listing("1"), _listing("2")], seen.__contains__)

    def test_empty_page_false(self):
        # An empty page must not be treated as "all seen" (would stop on a
        # transient empty response).
        assert not page_all_seen([], {"flatfox:1"}.__contains__)

    def test_no_predicate_false(self):
        # is_seen=None disables early-exit entirely.
        assert not page_all_seen([_listing("1")], None)


# ── Flatfox early-exit ─────────────────────────────────────────────────────────

def _fake_client(fetched_batches: list[list[int]]) -> MagicMock:
    """A stand-in httpx client that records requested PK batches and returns a
    listing object for each requested PK."""
    client = MagicMock()

    def _get(url, params=None, headers=None):
        pks = [int(v) for (k, v) in params if k == "pk"]
        fetched_batches.append(pks)
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "results": [
                {
                    "pk": pk, "url": f"/en/flat/{pk}/", "public_title": "t",
                    "rent_gross": 3000, "number_of_rooms": 3.0,
                    "zipcode": 8001, "city": "Zürich", "street": "S 1",
                }
                for pk in pks
            ]
        }
        return resp

    client.get.side_effect = _get
    return client


def _make_adapter(client: MagicMock) -> FlatfoxAdapter:
    adapter = FlatfoxAdapter(min_rooms=1.0, max_rent_chf=9999.0)
    adapter._client = client
    return adapter


def test_flatfox_stops_at_first_fully_seen_batch(monkeypatch):
    monkeypatch.setattr(flatfox, "_BATCH_SIZE", 2)
    fetched: list[list[int]] = []
    adapter = _make_adapter(_fake_client(fetched))
    # Pins come back unsorted; search() sorts them descending (newest-first).
    monkeypatch.setattr(adapter, "_fetch_pks", lambda: [1, 2, 3, 4, 5])

    # PKs 5 & 4 are new; the older ones are already seen.
    seen = {"flatfox:1", "flatfox:2", "flatfox:3"}
    listings = adapter.search(is_seen=seen.__contains__)

    # Batch 1 = [5, 4] (new) → continue; batch 2 = [3, 2] (all seen) → stop.
    assert fetched == [[5, 4], [3, 2]]
    # PK 1 (batch 3) was never requested.
    assert [l.id for l in listings] == ["5", "4", "3", "2"]


def test_homegate_url_has_price_and_newest_sort():
    # Regression: `al` is living surface, not price — price must be ag/ah, and
    # the results must be sorted newest-first for early-exit to be valid.
    from urllib.parse import parse_qs, urlparse

    from flatbot.adapters.homegate import HomegateAdapter

    url = HomegateAdapter(min_rooms=3.0, min_rent_chf=2000, max_rent_chf=4000)._build_url(2)
    q = parse_qs(urlparse(url).query)
    assert q["ac"] == ["3"]           # min rooms
    assert q["ag"] == ["2000"]        # min rent
    assert q["ah"] == ["4000"]        # max rent
    assert q["ep"] == ["2"]           # page
    assert q["o"] == ["dateCreated-desc"]
    assert "al" not in q              # never use the surface-area param for price


def test_immoscout_url_has_newest_sort():
    from urllib.parse import parse_qs, urlparse

    from flatbot.adapters.immoscout import ImmoScout24Adapter

    url = ImmoScout24Adapter(min_rooms=3.0, min_rent_chf=2000, max_rent_chf=4000)._build_url(2)
    q = parse_qs(urlparse(url).query)
    assert q["nrf"] == ["3"]
    assert q["pf"] == ["2000"]
    assert q["pt"] == ["4000"]
    assert q["pn"] == ["2"]
    assert q["o"] == ["dateCreated-desc"]


def test_comparis_url_has_newest_sort():
    # Sort=3 is newest-first; Sort=11 (old value) was relevance with scrambled dates.
    from urllib.parse import parse_qs, urlparse

    from flatbot.adapters.comparis import ComparisAdapter

    url = ComparisAdapter(min_rooms=2.5, min_rent_chf=2000, max_rent_chf=3500)._build_url(1)
    q = parse_qs(urlparse(url).query)
    assert q["sort"] == ["3"]
    req = json.loads(q["requestobject"][0])
    assert req["Sort"] == 3
    assert req["PriceFrom"] == "2000"
    assert req["PriceTo"] == "3500"
    assert req["RoomsFrom"] == "2"


def test_newhome_url_has_newest_order():
    from flatbot.adapters.newhome import NewHomeAdapter

    url = NewHomeAdapter(min_rooms=2.5, min_rent_chf=2000, max_rent_chf=3500)._build_api_url(0)
    assert "&order=1" in url          # newest first (was order=0)
    assert "&priceMin=2000" in url
    assert "&priceMax=3500" in url


def test_flatfox_no_early_exit_scans_everything(monkeypatch):
    monkeypatch.setattr(flatfox, "_BATCH_SIZE", 2)
    fetched: list[list[int]] = []
    adapter = _make_adapter(_fake_client(fetched))
    monkeypatch.setattr(adapter, "_fetch_pks", lambda: [1, 2, 3, 4, 5])

    # With no seen-predicate (first run / seed) every batch is fetched.
    listings = adapter.search()

    assert fetched == [[5, 4], [3, 2], [1]]
    assert [l.id for l in listings] == ["5", "4", "3", "2", "1"]
