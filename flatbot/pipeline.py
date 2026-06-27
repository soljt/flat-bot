from __future__ import annotations

import logging

from .adapters.base import Adapter, Listing
from .config import Config
from .llm import fallback_body, fallback_subject, generate_email
from .notifier import Notifier
from .store import SeenStore
from . import sheets

log = logging.getLogger(__name__)


def _passes_filter(listing: Listing, cfg: Config) -> tuple[bool, str]:
    if listing.rooms is not None and listing.rooms < cfg.min_rooms:
        return False, f"rooms={listing.rooms} < min={cfg.min_rooms}"

    # Teaser / on-request prices pass the price gate — they're flagged in the email
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
) -> dict[str, int]:
    stats: dict[str, int] = {
        "seen": 0,
        "new": 0,
        "notified": 0,
        "skipped": 0,
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

            subject, body = generate_email(listing, cfg.anthropic_api_key)

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
            sheets.append_row(listing, cfg.google_sheets_id, cfg.google_service_account_json)

            # Mark as seen ONLY after a successful send
            store.add(listing.uid)
            stats["notified"] += 1

    log.info(
        "action=cycle_done seen=%d new=%d notified=%d skipped=%d errors=%d",
        stats["seen"],
        stats["new"],
        stats["notified"],
        stats["skipped"],
        stats["errors"],
    )
    return stats
