from unittest.mock import MagicMock

import pytest

from flatbot.adapters.base import Listing
from flatbot.config import Config
from flatbot.matchstore import MatchStore
from flatbot.notifier import Notifier
from flatbot.pipeline import _passes_filter, run_cycle
from flatbot.store import SeenStore


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_listing(**kwargs) -> Listing:
    defaults = dict(
        id="123",
        url="https://example.com/123",
        title="Test Listing",
        price_chf=3000.0,
        rooms=5.5,
        postcode="8001",
        address="Hauptstrasse 1, 8001 Zürich",
        available_from="2024-02-01",
        description="Schöne Wohnung.",
        platform="flatfox",
    )
    defaults.update(kwargs)
    return Listing(**defaults)


def make_config(**kwargs) -> Config:
    defaults = dict(
        anthropic_api_key="sk-test",
        resend_api_key="re_test",
        resend_from="bot@example.com",
        mailing_list=["a@b.com"],
    )
    defaults.update(kwargs)
    return Config(**defaults)


def make_store(tmp_path) -> SeenStore:
    return SeenStore(str(tmp_path / "seen.txt"))


def make_notifier() -> MagicMock:
    n = MagicMock(spec=Notifier)
    n.send.return_value = None
    return n


def make_adapter(listings: list[Listing]) -> MagicMock:
    a = MagicMock()
    a.name = "test"
    a.search.return_value = listings
    return a


# ── Filter logic ─────────────────────────────────────────────────────────────


class TestPassesFilter:
    def test_valid_listing_passes(self):
        ok, _ = _passes_filter(make_listing(), make_config())
        assert ok is True

    def test_too_few_rooms_rejected(self):
        ok, reason = _passes_filter(make_listing(rooms=4.5), make_config())
        assert ok is False
        assert "rooms" in reason

    def test_over_budget_rejected(self):
        ok, reason = _passes_filter(make_listing(price_chf=7000.0), make_config())
        assert ok is False
        assert "price" in reason

    def test_wrong_postcode_rejected(self):
        ok, reason = _passes_filter(make_listing(postcode="3001"), make_config())
        assert ok is False
        assert "postcode" in reason

    def test_teaser_price_passes_despite_high_amount(self):
        listing = make_listing(price_chf=7000.0, price_is_teaser=True)
        ok, _ = _passes_filter(listing, make_config())
        assert ok is True

    def test_price_on_request_passes(self):
        listing = make_listing(price_chf=None, price_on_request=True)
        ok, _ = _passes_filter(listing, make_config())
        assert ok is True

    def test_null_price_passes(self):
        ok, _ = _passes_filter(make_listing(price_chf=None), make_config())
        assert ok is True

    def test_null_rooms_passes(self):
        ok, _ = _passes_filter(make_listing(rooms=None), make_config())
        assert ok is True

    def test_null_postcode_passes(self):
        ok, _ = _passes_filter(make_listing(postcode=None), make_config())
        assert ok is True

    def test_below_min_price_rejected(self):
        ok, reason = _passes_filter(make_listing(price_chf=2500.0), make_config())
        assert ok is False
        assert "price" in reason

    def test_min_price_boundary_passes(self):
        # min_rent_chf default is 3000; price == 3000 should pass (strict <)
        ok, _ = _passes_filter(make_listing(price_chf=3000.0), make_config())
        assert ok is True

    def test_teaser_price_below_min_passes(self):
        # Teaser prices are never filtered by price bounds
        listing = make_listing(price_chf=1000.0, price_is_teaser=True)
        ok, _ = _passes_filter(listing, make_config())
        assert ok is True


# ── Full cycle ───────────────────────────────────────────────────────────────


class TestRunCycle:
    def test_new_match_is_notified_and_marked(self, tmp_path, monkeypatch):
        listing = make_listing()
        store = make_store(tmp_path)
        notifier = make_notifier()

        monkeypatch.setattr("flatbot.pipeline.generate_email", lambda l, k, s=None: ("Sub", "Body"))

        stats = run_cycle([make_adapter([listing])], store, notifier, make_config())

        assert stats["notified"] == 1
        notifier.send.assert_called_once()
        assert store.contains(listing.uid)

    def test_already_seen_id_is_skipped(self, tmp_path, monkeypatch):
        listing = make_listing()
        store = make_store(tmp_path)
        store.add(listing.uid)
        notifier = make_notifier()

        stats = run_cycle([make_adapter([listing])], store, notifier, make_config())

        assert stats["notified"] == 0
        notifier.send.assert_not_called()

    def test_send_failure_leaves_id_unseen(self, tmp_path, monkeypatch):
        """Critical: mark-as-seen must happen ONLY after a successful send."""
        listing = make_listing()
        store = make_store(tmp_path)
        notifier = make_notifier()
        notifier.send.side_effect = RuntimeError("smtp timeout")

        monkeypatch.setattr("flatbot.pipeline.generate_email", lambda l, k, s=None: ("Sub", "Body"))

        stats = run_cycle([make_adapter([listing])], store, notifier, make_config())

        assert stats["errors"] == 1
        assert not store.contains(listing.uid)

    def test_filtered_listing_never_notified(self, tmp_path):
        listing = make_listing(rooms=3.0)
        store = make_store(tmp_path)
        notifier = make_notifier()

        stats = run_cycle([make_adapter([listing])], store, notifier, make_config())

        assert stats["new"] == 0
        assert stats["notified"] == 0
        notifier.send.assert_not_called()

    def test_dry_run_does_not_send_or_mark(self, tmp_path, monkeypatch):
        listing = make_listing()
        store = make_store(tmp_path)
        notifier = make_notifier()

        monkeypatch.setattr("flatbot.pipeline.generate_email", lambda l, k, s=None: ("Sub", "Body"))

        stats = run_cycle(
            [make_adapter([listing])], store, notifier, make_config(), dry_run=True
        )

        assert stats["notified"] == 1
        notifier.send.assert_not_called()
        assert not store.contains(listing.uid)

    def test_adapter_error_does_not_crash_loop(self, tmp_path, monkeypatch):
        bad = MagicMock()
        bad.name = "broken"
        bad.search.side_effect = RuntimeError("connection refused")

        good_listing = make_listing()
        good = make_adapter([good_listing])

        store = make_store(tmp_path)
        notifier = make_notifier()

        monkeypatch.setattr("flatbot.pipeline.generate_email", lambda l, k, s=None: ("Sub", "Body"))

        stats = run_cycle([bad, good], store, notifier, make_config())

        assert stats["errors"] == 1
        assert stats["notified"] == 1

    def test_cross_platform_dedup_sends_once(self, tmp_path, monkeypatch):
        """Same apartment on two platforms → one email, second counted as deduped."""
        address = "Musterstrasse 1, 8001 Zürich"
        listing_ff = make_listing(
            id="ff1", platform="flatfox", address=address
        )
        listing_hg = make_listing(
            id="hg1", platform="homegate", address=address
        )

        store = make_store(tmp_path)
        ms = MatchStore(str(tmp_path / "matches.jsonl"))
        notifier = make_notifier()

        monkeypatch.setattr("flatbot.pipeline.generate_email", lambda l, k, s=None: ("Sub", "<p>Body</p>"))

        stats = run_cycle(
            [make_adapter([listing_ff]), make_adapter([listing_hg])],
            store,
            notifier,
            make_config(),
            match_store=ms,
        )

        assert stats["notified"] == 1
        assert stats["deduped"] == 1
        notifier.send.assert_called_once()
        # Both UIDs should be marked seen
        assert store.contains(listing_ff.uid)
        assert store.contains(listing_hg.uid)

    def test_seed_mode_logs_and_marks_but_does_not_send(self, tmp_path, monkeypatch):
        """Seed mode: sheet + seen written; email + LLM never called."""
        listing = make_listing()
        store = make_store(tmp_path)
        ms = MatchStore(str(tmp_path / "matches.jsonl"))
        notifier = make_notifier()

        # If generate_email were called it would raise — proving it isn't
        monkeypatch.setattr(
            "flatbot.pipeline.generate_email",
            lambda l, k: (_ for _ in ()).throw(AssertionError("LLM called in seed mode")),
        )

        stats = run_cycle(
            [make_adapter([listing])],
            store,
            notifier,
            make_config(),
            seed_mode=True,
            match_store=ms,
        )

        assert stats["seeded"] == 1
        assert stats["notified"] == 0
        notifier.send.assert_not_called()
        assert store.contains(listing.uid)
        # Match key should be registered in the match store
        key = ms.match_key(listing)
        assert key is not None
        assert ms.lookup(key) is not None

    def test_seed_mode_dedupes_across_platforms(self, tmp_path, monkeypatch):
        """Seed mode still deduplicates: same flat on two platforms → one seeded, one deduped."""
        address = "Seedstrasse 7, 8003 Zürich"
        listing_ff = make_listing(id="ff_s", platform="flatfox", address=address)
        listing_hg = make_listing(id="hg_s", platform="homegate", address=address)

        store = make_store(tmp_path)
        ms = MatchStore(str(tmp_path / "matches.jsonl"))
        notifier = make_notifier()

        monkeypatch.setattr(
            "flatbot.pipeline.generate_email",
            lambda l, k: (_ for _ in ()).throw(AssertionError("LLM called in seed mode")),
        )

        stats = run_cycle(
            [make_adapter([listing_ff]), make_adapter([listing_hg])],
            store,
            notifier,
            make_config(),
            seed_mode=True,
            match_store=ms,
        )

        assert stats["seeded"] == 1
        assert stats["deduped"] == 1
        assert stats["notified"] == 0
        notifier.send.assert_not_called()
        assert store.contains(listing_ff.uid)
        assert store.contains(listing_hg.uid)

    def test_cycle_summary_counts(self, tmp_path, monkeypatch):
        seen_listing = make_listing(id="seen")
        new_listing = make_listing(id="new")
        filtered_listing = make_listing(id="fil", rooms=2.0)

        store = make_store(tmp_path)
        store.add(seen_listing.uid)
        notifier = make_notifier()

        monkeypatch.setattr("flatbot.pipeline.generate_email", lambda l, k, s=None: ("Sub", "Body"))

        stats = run_cycle(
            [make_adapter([seen_listing, new_listing, filtered_listing])],
            store,
            notifier,
            make_config(),
        )

        assert stats["seen"] == 3
        assert stats["new"] == 1
        assert stats["notified"] == 1
        assert stats["skipped"] == 1
        assert stats["errors"] == 0
