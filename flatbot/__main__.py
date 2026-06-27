from __future__ import annotations

import argparse
import logging
import random
import sys
import time

from .config import load_config
from .logging_setup import configure
from .store import SeenStore
from .notifier import ResendNotifier
from .pipeline import run_cycle
from .adapters.cloudflare import FlareSolverrSession
from .adapters.flatfox import FlatfoxAdapter
from .adapters.homegate import HomegateAdapter
from . import sheets


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="flatbot",
        description="Notify-first flat-match bot for Zurich.",
    )
    parser.add_argument(
        "--one-shot",
        action="store_true",
        help="Run one cycle and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the pipeline without sending emails or recording seen IDs.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        metavar="LEVEL",
        help="Logging level (default: INFO).",
    )
    args = parser.parse_args()

    configure(args.log_level)
    log = logging.getLogger(__name__)

    try:
        cfg = load_config()
    except ValueError as exc:
        log.error("action=config_error error=%s", exc)
        sys.exit(1)

    store = SeenStore(cfg.seen_store_path)
    notifier = ResendNotifier(cfg.resend_api_key, cfg.resend_from)

    session = FlareSolverrSession(
        flaresolverr_url=cfg.flaresolverr_url,
        max_timeout_ms=cfg.flaresolverr_max_timeout_ms,
    )

    adapters = []
    if cfg.enable_flatfox:
        adapters.append(
            FlatfoxAdapter(min_rooms=cfg.min_rooms, max_rent_chf=cfg.max_rent_chf, session=session)
        )
    if cfg.enable_homegate:
        adapters.append(
            HomegateAdapter(min_rooms=cfg.min_rooms, max_rent_chf=cfg.max_rent_chf, session=session)
        )

    if not adapters:
        log.error("action=start_failed reason=no_adapters_enabled")
        sys.exit(1)

    if cfg.google_sheets_id and cfg.google_service_account_json:
        sheets.ensure_headers(cfg.google_sheets_id, cfg.google_service_account_json)

    log.info(
        "action=start adapters=%s one_shot=%s dry_run=%s",
        [a.name for a in adapters],
        args.one_shot,
        args.dry_run,
    )

    if args.one_shot:
        run_cycle(adapters, store, notifier, cfg, dry_run=args.dry_run)
        return

    while True:
        run_cycle(adapters, store, notifier, cfg, dry_run=args.dry_run)
        jitter = random.randint(
            -cfg.poll_jitter_min * 60,
            cfg.poll_jitter_min * 60,
        )
        sleep_secs = cfg.poll_interval_min * 60 + jitter
        log.info("action=sleeping seconds=%d", sleep_secs)
        time.sleep(sleep_secs)


if __name__ == "__main__":
    main()
