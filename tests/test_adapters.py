from flatbot.adapters.base import detect_no_wg, detect_price_on_request, detect_teaser_price
from flatbot.adapters.flatfox import _parse as ff_parse
from flatbot.adapters.homegate import _parse as hg_parse
import json


# ── Flatfox parser ──────────────────────────────────────────────────────────
# Fixtures use the PublicListing schema fields from /api/v1/public-listing/:
#   pk, public_title, description, number_of_rooms (str decimal), rent_gross,
#   zipcode (int), city, street, moving_date, price_display_type

def test_flatfox_basic():
    item = {
        "pk": 123456,
        "public_title": "5.5-Zimmer-Wohnung Zürich",
        "description": "Helle Wohnung in ruhiger Lage.",
        "number_of_rooms": "5.50",
        "rent_gross": 3200,
        "zipcode": 8001,
        "city": "Zürich",
        "street": "Bahnhofstrasse 1",
        "moving_date": "2024-02-01",
    }
    listing = ff_parse(item)
    assert listing is not None
    assert listing.id == "123456"
    assert listing.uid == "flatfox:123456"
    assert listing.platform == "flatfox"
    assert listing.rooms == 5.5
    assert listing.price_chf == 3200.0
    assert listing.postcode == "8001"
    assert listing.price_is_teaser is False
    assert listing.price_on_request is False
    assert listing.no_wg_clause is False


def test_flatfox_missing_pk_returns_none():
    assert ff_parse({"public_title": "No PK"}) is None


def test_flatfox_teaser_via_text():
    item = {
        "pk": 1,
        "public_title": "Wohnung ab CHF 2800",
        "description": "",
        "number_of_rooms": "5.00",
        "rent_gross": 2800,
        "zipcode": 8005,
    }
    listing = ff_parse(item)
    assert listing is not None
    assert listing.price_is_teaser is True


def test_flatfox_teaser_via_field():
    item = {
        "pk": 2,
        "public_title": "Wohnung",
        "description": "",
        "number_of_rooms": "5.00",
        "rent_gross": 2800,
        "zipcode": 8005,
        "price_display_type": "FROM",
    }
    listing = ff_parse(item)
    assert listing is not None
    assert listing.price_is_teaser is True


def test_flatfox_price_on_request():
    item = {
        "pk": 3,
        "public_title": "Grosse Wohnung",
        "description": "Preis auf Anfrage. Bitte kontaktieren.",
        "number_of_rooms": "6.00",
        "rent_gross": None,
        "zipcode": 8002,
    }
    listing = ff_parse(item)
    assert listing is not None
    assert listing.price_on_request is True
    assert listing.price_chf is None


def test_flatfox_no_wg_clause():
    item = {
        "pk": 4,
        "public_title": "Wohnung",
        "description": "Keine WG erwünscht. Nur Einzelpersonen.",
        "number_of_rooms": "5.50",
        "rent_gross": 3000,
        "zipcode": 8003,
    }
    listing = ff_parse(item)
    assert listing is not None
    assert listing.no_wg_clause is True


def test_flatfox_fallback_title():
    item = {"pk": 5, "description": "", "number_of_rooms": "5.00", "zipcode": 8004}
    listing = ff_parse(item)
    assert listing is not None
    assert "5.0R" in listing.title


def test_flatfox_title_fallback_chain():
    """short_title is used when public_title is absent."""
    item = {"pk": 6, "short_title": "Schöne Wohnung", "number_of_rooms": "4.50", "zipcode": 8001}
    listing = ff_parse(item)
    assert listing is not None
    assert listing.title == "Schöne Wohnung"


# ── Homegate parser ─────────────────────────────────────────────────────────


def test_homegate_basic():
    item = {
        "listing": {
            "id": "abc123",
            "localization": {
                "de": {
                    "title": "Schöne 5.5-Zimmer-Wohnung",
                    "description": "Helle Wohnung.",
                }
            },
            "characteristics": {"numberOfRooms": 5.5},
            "prices": {"rent": {"gross": 3500}},
            "address": {
                "postalCode": "8006",
                "city": "Zürich",
                "street": "Universitätstrasse",
                "houseNumber": "10",
            },
            "availableFrom": "2024-03-01",
            "platforms": {"homegate": {"listingUrl": "https://www.homegate.ch/rent/12345"}},
        }
    }
    listing = hg_parse(item)
    assert listing is not None
    assert listing.id == "abc123"
    assert listing.uid == "homegate:abc123"
    assert listing.platform == "homegate"
    assert listing.rooms == 5.5
    assert listing.price_chf == 3500.0
    assert listing.postcode == "8006"
    assert listing.url == "https://www.homegate.ch/rent/abc123"
    assert listing.no_wg_clause is False


def test_homegate_missing_id_returns_none():
    assert hg_parse({"listing": {"id": ""}}) is None
    assert hg_parse({}) is None


def test_homegate_teaser_via_rent_field():
    item = {
        "listing": {
            "id": "xyz",
            "characteristics": {"numberOfRooms": 5.0},
            "prices": {"rent": {"gross": 2500, "isFrom": True}},
            "address": {"postalCode": "8001"},
        }
    }
    listing = hg_parse(item)
    assert listing is not None
    assert listing.price_is_teaser is True


def test_homegate_no_wg_in_description():
    item = {
        "listing": {
            "id": "wg1",
            "localization": {"de": {"title": "Flat", "description": "No shared flat please."}},
            "characteristics": {"numberOfRooms": 5.5},
            "prices": {"rent": {"gross": 3000}},
            "address": {"postalCode": "8003"},
        }
    }
    listing = hg_parse(item)
    assert listing is not None
    assert listing.no_wg_clause is True


def test_homegate_flat_response_format():
    item = {
        "id": "flat99",
        "characteristics": {"numberOfRooms": 5.0},
        "prices": {"rent": {"gross": 3200}},
        "address": {"postalCode": "8004"},
    }
    listing = hg_parse(item)
    assert listing is not None
    assert listing.id == "flat99"


def test_homegate_locality_fallback():
    """address.locality should be accepted as a city alias."""
    item = {
        "listing": {
            "id": "loc1",
            "characteristics": {"numberOfRooms": 4.5},
            "prices": {"rent": {"gross": 2800}},
            "address": {"postalCode": "8001", "locality": "Zürich"},
        }
    }
    listing = hg_parse(item)
    assert listing is not None
    assert "Zürich" in (listing.address or "")


# ── Keyword detectors ────────────────────────────────────────────────────────


def test_detect_no_wg():
    assert detect_no_wg("Keine WG erwünscht.") is True
    assert detect_no_wg("no flatshare please") is True
    assert detect_no_wg("Schöne Wohnung zu vermieten.") is False


def test_detect_teaser_price():
    assert detect_teaser_price("Miete ab CHF 2000") is True
    assert detect_teaser_price("Starting from CHF 2500") is True
    assert detect_teaser_price("Miete CHF 3000") is False


def test_detect_price_on_request():
    assert detect_price_on_request("Preis auf Anfrage") is True
    assert detect_price_on_request("price on request - contact us") is True
    assert detect_price_on_request("Miete CHF 3000 pro Monat") is False
