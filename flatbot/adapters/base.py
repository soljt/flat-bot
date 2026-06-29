from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

_NO_WG_KW = [
    "keine wg",
    "keine wohngemeinschaft",
    "wg unerwünscht",
    "keine mitbewohner",
    "nur einzelpersonen",
    "einzelperson bevorzugt",
    "no flatshare",
    "no shared flat",
    "no room share",
]

_TEASER_KW = [
    "ab chf",
    "ab fr.",
    "ab fr ",
    "ab sfr",
    "von chf",
    "starting from chf",
    "starting from fr",
]

_POR_KW = [
    "preis auf anfrage",
    "price on request",
    "auf anfrage",
    "prix sur demande",
]


def _contains(text: str, keywords: list[str]) -> bool:
    t = text.lower()
    return any(kw in t for kw in keywords)


def detect_no_wg(text: str) -> bool:
    return _contains(text, _NO_WG_KW)


def detect_teaser_price(text: str) -> bool:
    return _contains(text, _TEASER_KW)


def detect_price_on_request(text: str) -> bool:
    return _contains(text, _POR_KW)


@dataclass
class Listing:
    id: str
    url: str
    title: str
    price_chf: float | None
    rooms: float | None
    postcode: str | None
    address: str | None
    available_from: str | None
    description: str
    platform: str
    price_is_teaser: bool = False
    price_on_request: bool = False
    no_wg_clause: bool = False

    @property
    def uid(self) -> str:
        return f"{self.platform}:{self.id}"


class Adapter(ABC):
    name: str

    @abstractmethod
    def search(self) -> list[Listing]:
        ...

    def get_available_from(self, url: str) -> str | None:
        """Fetch the detail page and return the availability date string, or None.

        Default returns None (search API already provides the date, or the
        platform doesn't expose one).  Browser-based adapters whose search API
        omits this field override this with a detail-page scrape.
        """
        return None
