"""
Offline fixture-based email-serialization tests.

For each platform we load one real raw record (captured and saved in
tests/fixtures/<platform>_raw.json), parse it through the module-level
_parse function, then verify:

  1. All email-template fields are populated and of the right type.
  2. fallback_body() renders successfully and the first href in the
     resulting HTML equals listing.url and contains the listing id.
  3. The available_from field: platforms that expose it (Flatfox, NewHome)
     must have it; platforms whose search APIs don't include it (Homegate,
     ImmoScout24, Comparis) must return None — that is documented intent,
     not a bug.

These tests run offline with no network access.  They lock in the parsing
behaviour of the real live format captured during development.  If a
platform changes its JSON schema the fixture should be refreshed and the
test updated to match the new field names.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from flatbot.adapters.flatfox import _parse as ff_parse
from flatbot.adapters.homegate import _parse as hg_parse, _extract_available_from_html as hg_extract_avail
from flatbot.adapters.immoscout import _parse as is_parse, _extract_available_from_html as is_extract_avail
from flatbot.adapters.newhome import _parse as nh_parse
from flatbot.adapters.comparis import _parse as comp_parse, _extract_available_from_ad
from flatbot.llm import fallback_body

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _first_href(body: str) -> str:
    """Extract the first href attribute from an HTML body string."""
    m = re.search(r'href="([^"]+)"', body)
    assert m, f"No href found in rendered email body (first 300 chars): {body[:300]!r}"
    return m.group(1)


# ── Flatfox ───────────────────────────────────────────────────────────────────

class TestFlatfoxSerialization:
    @pytest.fixture
    def listing(self):
        raw = _load("flatfox_raw.json")
        result = ff_parse(raw)
        assert result is not None, "Flatfox fixture failed to parse"
        return result

    def test_platform(self, listing):
        assert listing.platform == "flatfox"

    def test_id_is_set(self, listing):
        assert listing.id

    def test_title(self, listing):
        assert listing.title

    def test_rooms(self, listing):
        assert listing.rooms is not None

    def test_price_or_price_flag(self, listing):
        assert listing.price_chf is not None or listing.price_on_request or listing.price_is_teaser

    def test_postcode(self, listing):
        assert listing.postcode

    def test_address_with_street(self, listing):
        # Our Flatfox fixture has a street — verify address is more than postcode+city.
        assert listing.address
        assert any(c.isalpha() for c in listing.address)

    def test_url_format(self, listing):
        assert listing.url
        assert "flatfox.ch" in listing.url

    def test_listing_id_in_url(self, listing):
        assert listing.id in listing.url

    def test_available_from_is_populated(self, listing):
        # Our fixture has moving_date set; Flatfox exposes this field.
        assert listing.available_from is not None, (
            "Flatfox fixture has moving_date set; available_from should be parsed"
        )

    def test_email_href_matches_url(self, listing):
        body = fallback_body(listing)
        assert _first_href(body) == listing.url

    def test_email_renders_available_from(self, listing):
        body = fallback_body(listing)
        # 'not specified' means the field is missing; 'available_from' should be shown.
        assert "not specified" not in body.split("Available from:")[1].split("</li>")[0]


def test_flatfox_by_agreement_sets_available_from():
    """moving_date_type='agr' with no moving_date must produce 'by agreement'."""
    item = {
        "pk": 99,
        "url": "/en/flat/99/",
        "public_title": "Test flat",
        "rent_gross": 4000,
        "number_of_rooms": 5.5,
        "zipcode": 8001,
        "city": "Zürich",
        "street": "Musterstrasse 1",
        "moving_date_type": "agr",
        "moving_date": None,
    }
    listing = ff_parse(item)
    assert listing is not None
    assert listing.available_from == "by agreement"


def test_flatfox_specific_date_still_works():
    """moving_date_type='dat' must produce the date string."""
    item = {
        "pk": 100,
        "url": "/en/flat/100/",
        "public_title": "Test flat",
        "rent_gross": 4000,
        "number_of_rooms": 5.5,
        "zipcode": 8001,
        "city": "Zürich",
        "street": "Musterstrasse 1",
        "moving_date_type": "dat",
        "moving_date": "2027-06-01",
    }
    listing = ff_parse(item)
    assert listing is not None
    assert listing.available_from == "2027-06-01"


def test_flatfox_no_date_no_type_is_none():
    """No moving_date and no moving_date_type must produce None."""
    item = {
        "pk": 101,
        "url": "/en/flat/101/",
        "public_title": "Test flat",
        "rent_gross": 4000,
        "number_of_rooms": 5.5,
        "zipcode": 8001,
        "city": "Zürich",
        "street": "Musterstrasse 1",
        "moving_date_type": None,
        "moving_date": None,
    }
    listing = ff_parse(item)
    assert listing is not None
    assert listing.available_from is None


def test_flatfox_sofort_sets_available_from():
    """moving_date_type='imm' with no moving_date must produce 'sofort'."""
    item = {
        "pk": 102,
        "url": "/en/flat/102/",
        "public_title": "Test flat",
        "rent_gross": 4000,
        "number_of_rooms": 5.5,
        "zipcode": 8001,
        "city": "Zürich",
        "street": "Musterstrasse 1",
        "moving_date_type": "imm",
        "moving_date": None,
    }
    listing = ff_parse(item)
    assert listing is not None
    assert listing.available_from == "sofort"


# ── Homegate ──────────────────────────────────────────────────────────────────

class TestHomgateSerialization:
    @pytest.fixture
    def listing(self):
        raw = _load("homegate_raw.json")
        result = hg_parse(raw)
        assert result is not None, "Homegate fixture failed to parse"
        return result

    def test_platform(self, listing):
        assert listing.platform == "homegate"

    def test_id_is_set(self, listing):
        assert listing.id

    def test_title(self, listing):
        assert listing.title

    def test_rooms(self, listing):
        assert listing.rooms is not None

    def test_price_or_price_flag(self, listing):
        assert listing.price_chf is not None or listing.price_on_request or listing.price_is_teaser

    def test_postcode(self, listing):
        assert listing.postcode

    def test_address(self, listing):
        assert listing.address

    def test_url_format(self, listing):
        assert listing.url
        assert "homegate.ch" in listing.url

    def test_listing_id_in_url(self, listing):
        assert listing.id in listing.url

    def test_available_from_is_none(self, listing):
        # Homegate's search API (via __INITIAL_STATE__) does not include an
        # availability date field.  None is the correct, documented value here.
        assert listing.available_from is None, (
            "Homegate search API does not expose availableFrom; expected None"
        )

    def test_email_href_matches_url(self, listing):
        body = fallback_body(listing)
        assert _first_href(body) == listing.url


# ── ImmoScout24 ───────────────────────────────────────────────────────────────

class TestImmoScoutSerialization:
    @pytest.fixture
    def listing(self):
        raw = _load("immoscout_raw.json")
        result = is_parse(raw)
        assert result is not None, "ImmoScout fixture failed to parse"
        return result

    def test_platform(self, listing):
        assert listing.platform == "immoscout24"

    def test_id_is_set(self, listing):
        assert listing.id

    def test_title(self, listing):
        assert listing.title

    def test_rooms(self, listing):
        assert listing.rooms is not None

    def test_price_or_price_flag(self, listing):
        assert listing.price_chf is not None or listing.price_on_request or listing.price_is_teaser

    def test_postcode(self, listing):
        assert listing.postcode

    def test_address(self, listing):
        assert listing.address

    def test_url_format(self, listing):
        assert listing.url
        assert "immoscout24.ch" in listing.url

    def test_listing_id_in_url(self, listing):
        assert listing.id in listing.url

    def test_available_from_is_none(self, listing):
        # IS24 search API (same SMG schema as Homegate) does not include
        # an availability date in the __INITIAL_STATE__ data.
        assert listing.available_from is None, (
            "IS24 search API does not expose availableFrom; expected None"
        )

    def test_email_href_matches_url(self, listing):
        body = fallback_body(listing)
        assert _first_href(body) == listing.url


# ── NewHome ───────────────────────────────────────────────────────────────────

class TestNewHomeSerialization:
    @pytest.fixture
    def listing(self):
        raw = _load("newhome_raw.json")
        result = nh_parse(raw)
        assert result is not None, "NewHome fixture failed to parse"
        return result

    def test_platform(self, listing):
        assert listing.platform == "newhome"

    def test_id_is_set(self, listing):
        assert listing.id

    def test_title(self, listing):
        assert listing.title

    def test_rooms(self, listing):
        assert listing.rooms is not None

    def test_price_or_price_flag(self, listing):
        assert listing.price_chf is not None or listing.price_on_request or listing.price_is_teaser

    def test_postcode(self, listing):
        assert listing.postcode

    def test_address_with_street(self, listing):
        # Our fixture has a street address.
        assert listing.address

    def test_url_format(self, listing):
        assert listing.url
        assert "newhome.ch" in listing.url

    def test_listing_id_in_url(self, listing):
        assert str(listing.id) in listing.url

    def test_available_from_is_populated(self, listing):
        # NewHome exposes availabilityDate; our fixture has it set.
        assert listing.available_from is not None, (
            "NewHome fixture has availabilityDate set; available_from should be parsed"
        )

    def test_available_from_is_date_string(self, listing):
        # Should be truncated to YYYY-MM-DD (first 10 chars of ISO8601).
        assert len(listing.available_from) == 10
        assert re.match(r"\d{4}-\d{2}-\d{2}", listing.available_from), (
            f"available_from should be YYYY-MM-DD, got {listing.available_from!r}"
        )

    def test_email_href_matches_url(self, listing):
        body = fallback_body(listing)
        assert _first_href(body) == listing.url

    def test_email_renders_available_from(self, listing):
        body = fallback_body(listing)
        after = body.split("Available from:")[1].split("</li>")[0]
        assert "not specified" not in after, (
            "available_from should appear in email, not 'not specified'"
        )


# ── Comparis ──────────────────────────────────────────────────────────────────

class TestComparisSerialization:
    @pytest.fixture
    def listing(self):
        raw = _load("comparis_raw.json")
        result = comp_parse(raw)
        assert result is not None, "Comparis fixture failed to parse"
        return result

    def test_platform(self, listing):
        assert listing.platform == "comparis"

    def test_id_is_set(self, listing):
        assert listing.id

    def test_title(self, listing):
        assert listing.title

    def test_rooms(self, listing):
        assert listing.rooms is not None

    def test_price_or_price_flag(self, listing):
        assert listing.price_chf is not None or listing.price_on_request or listing.price_is_teaser

    def test_postcode(self, listing):
        assert listing.postcode

    def test_url_format(self, listing):
        assert listing.url
        assert "comparis.ch" in listing.url

    def test_listing_id_in_url(self, listing):
        assert listing.id in listing.url

    def test_available_from_is_none(self, listing):
        # Comparis search results do not include a per-listing availability
        # date.  The 'Date' field in the raw data is the listing creation
        # timestamp, not move-in availability.
        assert listing.available_from is None, (
            "Comparis does not expose an availability date; expected None"
        )

    def test_description_is_string_not_none(self, listing):
        # Regression for a bug where empty Remarks produced None instead of "".
        assert isinstance(listing.description, str)

    def test_email_href_matches_url(self, listing):
        body = fallback_body(listing)
        assert _first_href(body) == listing.url


# ── Detail-page extraction helpers (offline unit tests) ──────────────────────


class TestHomegateHtmlExtraction:
    """Unit tests for the module-level HTML regex helper — no browser needed."""

    def test_finds_by_agreement_from_dt_dd(self):
        html = "<dt>Available from:</dt><dd>By agreement</dd>"
        assert hg_extract_avail(html) == "By agreement"

    def test_finds_immediately_from_dt_dd(self):
        html = "<dt>Available from:</dt><dd>Immediately</dd>"
        assert hg_extract_avail(html) == "Immediately"

    def test_finds_date_verbatim_from_dt_dd(self):
        html = "<dt>Available from:</dt><dd>01.09.2026</dd>"
        assert hg_extract_avail(html) == "01.09.2026"

    def test_dt_dd_case_insensitive(self):
        html = "<dt>available from:</dt><dd>By agreement</dd>"
        assert hg_extract_avail(html) == "By agreement"

    def test_dt_dd_with_whitespace(self):
        html = "<dt>Available from:</dt>\n  <dd>By agreement</dd>"
        assert hg_extract_avail(html) == "By agreement"

    def test_fallback_finds_iso_date_in_json(self):
        # JSON fallback: used when <dt>/<dd> pattern is absent
        html = '"lister":{"id":"x"},"availableFrom":"2026-09-01","characteristics":{}'
        assert hg_extract_avail(html) == "2026-09-01"

    def test_returns_none_when_absent(self):
        assert hg_extract_avail("<html>no date here</html>") is None

    def test_json_fallback_does_not_match_non_iso_format(self):
        # German dd.mm.yyyy embedded as JSON value should NOT be captured
        assert hg_extract_avail('"availableFrom":"01.09.2026"') is None


class TestImmoScoutHtmlExtraction:
    """IS24 uses German <dt>Verfügbarkeit:</dt><dd>...</dd> pattern."""

    def test_finds_nach_vereinbarung_from_dt_dd(self):
        html = "<dt>Verfügbarkeit:</dt><dd>Nach Vereinbarung</dd>"
        assert is_extract_avail(html) == "Nach Vereinbarung"

    def test_finds_sofort_from_dt_dd(self):
        html = "<dt>Verfügbarkeit:</dt><dd>Sofort</dd>"
        assert is_extract_avail(html) == "Sofort"

    def test_finds_date_verbatim_from_dt_dd(self):
        html = "<dt>Verfügbarkeit:</dt><dd>01.08.2026</dd>"
        assert is_extract_avail(html) == "01.08.2026"

    def test_fallback_finds_iso_date_in_json(self):
        html = '"availableFrom":"2026-08-01","other":"x"'
        assert is_extract_avail(html) == "2026-08-01"

    def test_returns_none_when_absent(self):
        assert is_extract_avail("no date") is None


class TestComparisAdExtraction:
    """Unit tests for the Comparis __NEXT_DATA__ ad object extraction."""

    def test_sofort_returned_as_is(self):
        ad = {"MainData": [{"Key": "AvailableDate", "Value": "sofort"}]}
        assert _extract_available_from_ad(ad) == "sofort"

    def test_german_date_normalized_to_iso(self):
        ad = {"MainData": [{"Key": "AvailableDate", "Value": "01.09.2026"}]}
        assert _extract_available_from_ad(ad) == "2026-09-01"

    def test_returns_none_when_key_absent(self):
        ad = {"MainData": [{"Key": "NumRooms", "Value": "5.5"}]}
        assert _extract_available_from_ad(ad) is None

    def test_returns_none_when_main_data_missing(self):
        assert _extract_available_from_ad({}) is None

    def test_returns_none_when_value_empty(self):
        ad = {"MainData": [{"Key": "AvailableDate", "Value": ""}]}
        assert _extract_available_from_ad(ad) is None

    def test_skips_other_keys_before_available_date(self):
        ad = {
            "MainData": [
                {"Key": "PropertyType", "Value": "Wohnung"},
                {"Key": "AvailableDate", "Value": "15.10.2026"},
            ]
        }
        assert _extract_available_from_ad(ad) == "2026-10-15"
