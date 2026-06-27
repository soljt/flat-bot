import pytest

from flatbot.adapters.base import Listing
from flatbot.matchstore import MatchStore, _normalize_address


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_listing(**kwargs) -> Listing:
    defaults = dict(
        id="123",
        url="https://example.com/123",
        title="Test",
        price_chf=3500.0,
        rooms=5.5,
        postcode="8001",
        address="Hauptstrasse 1, 8001 Zürich",
        available_from=None,
        description="",
        platform="flatfox",
    )
    defaults.update(kwargs)
    return Listing(**defaults)


# ── Address normalisation ─────────────────────────────────────────────────────


def test_normalize_address_lowercases():
    assert _normalize_address("Hauptstrasse 1, 8001 Zürich") == "hauptstrasse 1 8001 zürich"


def test_normalize_address_strips_punctuation():
    result = _normalize_address("Bahnhof-Str. 12a, 8004 Zürich!")
    assert "." not in result
    assert "!" not in result
    assert "," not in result


def test_normalize_address_collapses_spaces():
    result = _normalize_address("A   B")
    assert "  " not in result


# ── match_key ─────────────────────────────────────────────────────────────────


def test_match_key_uses_address_when_present(tmp_path):
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    listing = make_listing(address="Musterstrasse 5, 8002 Zürich")
    key = ms.match_key(listing)
    assert key is not None
    assert key.startswith("addr:")


def test_match_key_address_platform_independent(tmp_path):
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    ff = make_listing(platform="flatfox", address="Musterstrasse 5, 8002 Zürich")
    hg = make_listing(platform="homegate", address="Musterstrasse 5, 8002 Zürich")
    assert ms.match_key(ff) == ms.match_key(hg)


def test_match_key_falls_back_to_bucket(tmp_path):
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    listing = make_listing(address=None, postcode="8003", rooms=5.5, price_chf=3500.0)
    key = ms.match_key(listing)
    assert key is not None
    assert key.startswith("bucket:")


def test_match_key_bucket_groups_nearby_prices(tmp_path):
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    # 3500 and 3510 should land in the same CHF-50 bucket (both → bucket 70)
    l1 = make_listing(address=None, postcode="8001", rooms=5.5, price_chf=3500.0)
    l2 = make_listing(address=None, postcode="8001", rooms=5.5, price_chf=3510.0)
    assert ms.match_key(l1) == ms.match_key(l2)


def test_match_key_returns_none_when_insufficient_data(tmp_path):
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    listing = make_listing(address=None, postcode=None, rooms=None, price_chf=None)
    assert ms.match_key(listing) is None


# ── lookup / add ──────────────────────────────────────────────────────────────


def test_add_and_lookup(tmp_path):
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    ms.add("mykey", "flatfox:1", "https://flatfox.ch/1", sheet_row=3)
    record = ms.lookup("mykey")
    assert record is not None
    assert record.uid == "flatfox:1"
    assert record.url == "https://flatfox.ch/1"
    assert record.sheet_row == 3


def test_lookup_returns_none_for_unknown_key(tmp_path):
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    assert ms.lookup("ghost") is None


# ── Persistence ───────────────────────────────────────────────────────────────


def test_records_survive_reload(tmp_path):
    path = str(tmp_path / "m.jsonl")
    ms1 = MatchStore(path)
    ms1.add("k1", "flatfox:1", "https://flatfox.ch/1", sheet_row=2)
    ms1.add("k2", "homegate:99", "https://homegate.ch/99", sheet_row=None)

    ms2 = MatchStore(path)
    r1 = ms2.lookup("k1")
    r2 = ms2.lookup("k2")
    assert r1 is not None and r1.uid == "flatfox:1" and r1.sheet_row == 2
    assert r2 is not None and r2.uid == "homegate:99" and r2.sheet_row is None


def test_bad_lines_are_skipped_on_load(tmp_path):
    path = str(tmp_path / "m.jsonl")
    with open(path, "w") as f:
        f.write("not json\n")
        f.write('{"key": "k1", "uid": "u", "url": "http://x", "sheet_row": null}\n')

    ms = MatchStore(path)
    assert ms.lookup("k1") is not None  # good line loaded
