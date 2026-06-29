import pytest

from flatbot.adapters.base import Listing
from flatbot.matchstore import MatchStore, _normalize_address, _is_real_street_address, _extract_street_number


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


# ── match_keys — all applicable keys ─────────────────────────────────────────


def test_match_keys_full_listing_returns_four_keys(tmp_path):
    """A listing with real address + title + price produces all four key types."""
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    listing = make_listing()  # real addr, title, postcode, rooms, price
    keys = ms.match_keys(listing)
    assert len(keys) == 4
    assert keys[0].startswith("addr:")
    assert keys[1].startswith("street:")
    assert keys[2].startswith("title:")
    assert keys[3].startswith("bucket:")


def test_match_keys_no_real_addr_returns_title_and_bucket(tmp_path):
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    listing = make_listing(address="8001 Zürich")  # bare postcode+city — not real
    keys = ms.match_keys(listing)
    assert all(not k.startswith("addr:") for k in keys)
    assert any(k.startswith("title:") for k in keys)


def test_match_keys_no_title_returns_addr_and_bucket(tmp_path):
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    listing = make_listing(title="")
    keys = ms.match_keys(listing)
    assert keys[0].startswith("addr:")
    assert all(not k.startswith("title:") for k in keys)


def test_match_keys_empty_when_insufficient_data(tmp_path):
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    listing = make_listing(title="", address=None, postcode=None, rooms=None, price_chf=None)
    assert ms.match_keys(listing) == []


# ── match_key — primary key (first of match_keys) ────────────────────────────


def test_match_key_addr_is_primary_when_real_address_present(tmp_path):
    """addr: is the most specific key and always comes first."""
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    listing = make_listing(address="Musterstrasse 5, 8002 Zürich")
    key = ms.match_key(listing)
    assert key is not None
    assert key.startswith("addr:")


def test_match_key_title_is_primary_without_real_address(tmp_path):
    """When address is bare postcode+city, title: becomes the primary key."""
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    listing = make_listing(address="8001 Zürich")  # not real
    key = ms.match_key(listing)
    assert key is not None
    assert key.startswith("title:")


def test_match_key_falls_back_to_bucket(tmp_path):
    """Without title or real street address, primary key is bucket:."""
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    listing = make_listing(title="", address=None, postcode="8003", rooms=5.5, price_chf=3500.0)
    key = ms.match_key(listing)
    assert key is not None
    assert key.startswith("bucket:")


def test_match_key_bucket_groups_nearby_prices(tmp_path):
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    # 3500 and 3510 both round to bucket 70 (round(x/50))
    l1 = make_listing(title="", address=None, postcode="8001", rooms=5.5, price_chf=3500.0)
    l2 = make_listing(title="", address=None, postcode="8001", rooms=5.5, price_chf=3510.0)
    assert ms.match_key(l1) == ms.match_key(l2)


def test_match_key_returns_none_when_insufficient_data(tmp_path):
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    listing = make_listing(title="", address=None, postcode=None, rooms=None, price_chf=None)
    assert ms.match_key(listing) is None


# ── lookup_any — tries all keys ───────────────────────────────────────────────


def test_lookup_any_finds_by_addr_key(tmp_path):
    """Standard case: same address on two platforms → found via addr: key."""
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    listing = make_listing(id="1", platform="flatfox")
    for key in ms.match_keys(listing):
        ms.add(key, listing.uid, listing.url)
    other = make_listing(id="2", platform="homegate")  # same default address
    assert ms.lookup_any(other) is not None


def test_lookup_any_finds_by_title_key_when_address_differs(tmp_path):
    """Cross-platform dedup via title+postcode+rooms when address granularity differs."""
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    is24 = make_listing(
        id="is1",
        platform="immoscout24",
        title="Luxuriöses Apartment in 8008 Zürich",
        address="Rennweg 35, 8008 Zürich",
        postcode="8008",
        rooms=5.5,
        price_chf=5000.0,
    )
    newhome = make_listing(
        id="nh1",
        platform="newhome",
        title="Luxuriöses Apartment in 8008 Zürich",
        address="8008 Zürich",   # bare postcode only — no real addr: key generated
        postcode="8008",
        rooms=5.5,
        price_chf=5000.0,
    )
    # IS24 stored first — under addr: + title: + bucket:
    for key in ms.match_keys(is24):
        ms.add(key, is24.uid, is24.url)
    # NewHome has no addr: key but shares the title: key → should find IS24's entry
    assert ms.lookup_any(newhome) is not None


def test_lookup_any_finds_by_addr_key_even_when_titles_differ(tmp_path):
    """Flatfox (English) vs Homegate (German) same flat: addr: key bridges the gap."""
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    ff = make_listing(
        id="ff1",
        platform="flatfox",
        title="Exclusive attic flat with panoramic view",
        address="Im oberen Boden 142, 8049 Zürich",
        postcode="8049",
        rooms=5.5,
        price_chf=4800.0,
    )
    hg = make_listing(
        id="hg1",
        platform="homegate",
        title="Exklusive, grosszügige Attikawohnung mit traumhafter Aussicht",
        address="Im oberen Boden 142, 8049 Zürich",
        postcode="8049",
        rooms=5.5,
        price_chf=4800.0,
    )
    for key in ms.match_keys(ff):
        ms.add(key, ff.uid, ff.url)
    # Homegate has different title → different title: key, but same addr: key
    assert ms.lookup_any(hg) is not None


def test_lookup_any_returns_none_for_genuinely_different_listing(tmp_path):
    """Different address AND different title → no match."""
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    a = make_listing(id="1", title="Wohnung A", address="Seestrasse 1, 8002 Zürich",
                     postcode="8002", price_chf=4000.0)
    b = make_listing(id="2", title="Wohnung B", address="Bergstrasse 5, 8032 Zürich",
                     postcode="8032", price_chf=4500.0)
    for key in ms.match_keys(a):
        ms.add(key, a.uid, a.url)
    assert ms.lookup_any(b) is None


def test_match_key_platform_independent_via_addr(tmp_path):
    """Two platforms with same real address always share the primary addr: key."""
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    ff = make_listing(platform="flatfox", address="Musterstrasse 5, 8002 Zürich")
    hg = make_listing(platform="homegate", address="Musterstrasse 5, 8002 Zürich")
    assert ms.match_key(ff) == ms.match_key(hg)
    assert ms.match_key(ff).startswith("addr:")


def test_match_key_price_on_request_deduplicates_by_addr(tmp_path):
    """price_on_request listings still dedup via addr: (no bucket: needed)."""
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    a = make_listing(platform="homegate", price_chf=None, price_on_request=True)
    b = make_listing(platform="newhome", price_chf=None, price_on_request=True)
    # Same default real address → same addr: key
    assert ms.match_key(a) == ms.match_key(b)
    assert ms.match_key(a) is not None


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


# ── _is_real_street_address ──────────────────────────────────────────────────


def test_real_street_with_number():
    assert _is_real_street_address("Bahnhofstrasse 1, 8001 Zürich") is True


def test_real_street_with_alphanumeric_number():
    assert _is_real_street_address("Hauptstrasse 12a, 8044 Zürich") is True


def test_real_street_without_city():
    assert _is_real_street_address("Seestrasse 7") is True


def test_postcode_city_only_is_not_real():
    assert _is_real_street_address("8044 Zürich") is False


def test_another_postcode_city_only_is_not_real():
    assert _is_real_street_address("8006 Zürich") is False


def test_street_without_house_number_is_not_real():
    # Street name present but no digit after postcode removal — cannot dedup safely.
    assert _is_real_street_address("Bahnhofstrasse, 8001 Zürich") is False


# ── _extract_street_number ────────────────────────────────────────────────────


def test_extract_street_number_strips_postcode_and_city():
    assert _extract_street_number("Musterstrasse 12, 8006 Zürich") == "musterstrasse 12"


def test_extract_street_number_no_postcode_returns_normalized():
    assert _extract_street_number("Musterstrasse 12") == "musterstrasse 12"


def test_extract_street_number_matches_across_platforms():
    """Full address and street-only address produce the same key."""
    assert (
        _extract_street_number("Musterstrasse 12, 8006 Zürich")
        == _extract_street_number("Musterstrasse 12")
    )


# ── street: key — Comparis cross-platform dedup ───────────────────────────────


def test_lookup_any_finds_comparis_listing_via_street_key(tmp_path):
    """Comparis returns street+number only; other platform has full address — should dedup."""
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    homegate = make_listing(
        id="hg1",
        platform="homegate",
        title="Schöne Wohnung an zentraler Lage",
        address="Musterstrasse 12, 8006 Zürich",
        postcode="8006",
        rooms=5.5,
        price_chf=4000.0,
    )
    comparis = make_listing(
        id="cp1",
        platform="comparis",
        title="Schöne Wohnung an zentraler Lage",
        address="Musterstrasse 12",   # no postcode in Comparis address field
        postcode=None,
        rooms=5.5,
        price_chf=4000.0,
    )
    for key in ms.match_keys(homegate):
        ms.add(key, homegate.uid, homegate.url)
    assert ms.lookup_any(comparis) is not None


def test_lookup_any_comparis_first_then_homegate(tmp_path):
    """If Comparis is processed first, Homegate should still dedup against it."""
    ms = MatchStore(str(tmp_path / "m.jsonl"))
    comparis = make_listing(
        id="cp1",
        platform="comparis",
        title="Apartment Zürich",
        address="Bahnhofstrasse 5",
        postcode=None,
        rooms=5.5,
        price_chf=3800.0,
    )
    homegate = make_listing(
        id="hg1",
        platform="homegate",
        title="Apartment Zürich",
        address="Bahnhofstrasse 5, 8001 Zürich",
        postcode="8001",
        rooms=5.5,
        price_chf=3800.0,
    )
    for key in ms.match_keys(comparis):
        ms.add(key, comparis.uid, comparis.url)
    assert ms.lookup_any(homegate) is not None
