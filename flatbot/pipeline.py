from __future__ import annotations

import dataclasses
import logging

from .adapters.base import Adapter, Listing
from .config import Config
from .llm import generate_email
from .matchstore import MatchStore
from .notifier import Notifier
from .store import SeenStore
from . import sheets

log = logging.getLogger(__name__)


def _sheets_url(cfg: Config) -> str | None:
    if not cfg.google_sheets_id:
        return None
    return f"https://docs.google.com/spreadsheets/d/{cfg.google_sheets_id}/edit"


def _passes_filter(listing: Listing, cfg: Config) -> tuple[bool, str]:
    if listing.rooms is not None and listing.rooms < cfg.min_rooms:
        return False, f"rooms={listing.rooms} < min={cfg.min_rooms}"

    # Teaser / on-request prices pass the price gate — they're flagged in the email
    if (
        listing.price_chf is not None
        and not listing.price_is_teaser
        and not listing.price_on_request
        and listing.price_chf < cfg.min_rent_chf
    ):
        return False, f"price={listing.price_chf} < min={cfg.min_rent_chf}"

    if (
        listing.price_chf is not None
        and not listing.price_is_teaser
        and not listing.price_on_request
        and listing.price_chf > cfg.max_rent_chf
    ):
        return False, f"price={listing.price_chf} > max={cfg.max_rent_chf}"

    if listing.postcode and not listing.postcode.startswith(cfg.postcode_prefix):
        return False, f"postcode={listing.postcode} not in {cfg.postcode_prefix}xx"

    return True, ""


def run_cycle(
    adapters: list[Adapter],
    store: SeenStore,
    notifier: Notifier,
    cfg: Config,
    dry_run: bool = False,
    match_store: MatchStore | None = None,
    seed_mode: bool = False,
) -> dict[str, int]:
    stats: dict[str, int] = {
        "seen": 0,
        "new": 0,
        "notified": 0,
        "seeded": 0,
        "skipped": 0,
        "deduped": 0,
        "errors": 0,
    }

    for adapter in adapters:
        try:
            listings = adapter.search()
        except Exception:
            log.error(
                "platform=%s action=adapter_failed",
                adapter.name,
                exc_info=True,
            )
            stats["errors"] += 1
            continue

        for listing in listings:
            stats["seen"] += 1

            if store.contains(listing.uid):
                continue

            passes, reason = _passes_filter(listing, cfg)
            if not passes:
                log.debug(
                    "platform=%s action=filtered id=%s reason=%s",
                    listing.platform,
                    listing.id,
                    reason,
                )
                stats["skipped"] += 1
                continue

            stats["new"] += 1
            log.info(
                "platform=%s action=new_match id=%s rooms=%s price=%s postcode=%s title=%r",
                listing.platform,
                listing.id,
                listing.rooms,
                listing.price_chf,
                listing.postcode,
                listing.title[:60],
            )

            if dry_run:
                log.info("platform=%s action=dry_run id=%s", listing.platform, listing.id)
                stats["notified"] += 1
                continue

            # Compute all dedup keys once; used for lookup and registration below.
            # All keys are tried on lookup and stored on write so that two listings
            # for the same flat match even when they share only address OR only title.
            listing_keys = match_store.match_keys(listing) if match_store is not None else []

            # ── Cross-platform dedup ──────────────────────────────────────
            if match_store is not None and listing_keys:
                existing = match_store.lookup_any(listing)
                if existing is not None:
                    log.info(
                        "platform=%s action=deduped id=%s matched_uid=%s",
                        listing.platform,
                        listing.id,
                        existing.uid,
                    )
                    if existing.sheet_row is not None:
                        sheets.add_other_platform_link(
                            cfg.google_sheets_id,
                            cfg.google_service_account_json,
                            existing.sheet_row,
                            listing.url,
                        )
                    store.add(listing.uid)
                    stats["deduped"] += 1
                    continue

            # ── Enrich: detail-page scrape for available_from ─────────────────
            # Fires for any new listing that will generate a sheet row (seed
            # or normal run) — never for dry runs (short-circuited above).
            if listing.available_from is None:
                try:
                    avail = adapter.get_available_from(listing.url)
                    if avail:
                        listing = dataclasses.replace(listing, available_from=avail)
                        log.debug(
                            "platform=%s action=enriched id=%s available_from=%s",
                            listing.platform, listing.id, avail,
                        )
                except Exception:
                    log.warning(
                        "platform=%s action=enrich_failed id=%s",
                        listing.platform, listing.id,
                        exc_info=True,
                    )

            # ── Seed mode: log to sheet + mark seen, no email / LLM ──────
            if seed_mode:
                log.info("platform=%s action=seed id=%s", listing.platform, listing.id)
                sheet_row = sheets.append_row(
                    listing, cfg.google_sheets_id, cfg.google_service_account_json
                )
                if match_store is not None and listing_keys:
                    for _key in listing_keys:
                        match_store.add(_key, listing.uid, listing.url, sheet_row)
                store.add(listing.uid)
                stats["seeded"] += 1
                continue

            # ── Normal notify path ────────────────────────────────────────
            subject, body = generate_email(listing, cfg.anthropic_api_key, _sheets_url(cfg))

            try:
                notifier.send(
                    subject=subject,
                    body=body,
                    recipients=cfg.mailing_list,
                )
            except Exception:
                log.error(
                    "platform=%s action=send_failed id=%s",
                    listing.platform,
                    listing.id,
                    exc_info=True,
                )
                stats["errors"] += 1
                continue

            # Sheets is best-effort: failure is logged but does NOT prevent marking seen
            sheet_row = sheets.append_row(
                listing, cfg.google_sheets_id, cfg.google_service_account_json
            )

            # Register in match store ONLY after a successful send
            if match_store is not None and listing_keys:
                for _key in listing_keys:
                    match_store.add(_key, listing.uid, listing.url, sheet_row)

            # Mark as seen ONLY after a successful send
            store.add(listing.uid)
            stats["notified"] += 1

    log.info(
        "action=cycle_done seen=%d new=%d notified=%d seeded=%d skipped=%d deduped=%d errors=%d",
        stats["seen"],
        stats["new"],
        stats["notified"],
        stats["seeded"],
        stats["skipped"],
        stats["deduped"],
        stats["errors"],
    )
    return stats
