from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Required
    anthropic_api_key: str
    resend_api_key: str
    resend_from: str
    mailing_list: list[str]

    # Search criteria
    min_rooms: float = 5.0
    max_rent_chf: float = 6500.0
    postcode_prefix: str = "80"

    # Platform toggles
    enable_flatfox: bool = True
    enable_homegate: bool = True

    # Poll schedule
    poll_interval_min: int = 15
    poll_jitter_min: int = 5

    # Google Sheets (optional)
    google_sheets_id: str = ""
    google_service_account_json: str = ""

    # FlareSolverr (Cloudflare bypass)
    flaresolverr_url: str = "http://localhost:8191/v1"
    flaresolverr_max_timeout_ms: int = 60_000

    # Storage
    seen_store_path: str = "seen.txt"


def load_config() -> Config:
    mailing_list = [
        e.strip()
        for e in os.getenv("MAILING_LIST", "").split(",")
        if e.strip()
    ]

    missing = []
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    resend_key = os.getenv("RESEND_API_KEY", "")
    resend_from = os.getenv("RESEND_FROM", "")

    if not anthropic_key:
        missing.append("ANTHROPIC_API_KEY")
    if not resend_key:
        missing.append("RESEND_API_KEY")
    if not resend_from:
        missing.append("RESEND_FROM")
    if not mailing_list:
        missing.append("MAILING_LIST")

    if missing:
        raise ValueError(f"Required env vars not set: {', '.join(missing)}")

    return Config(
        anthropic_api_key=anthropic_key,
        resend_api_key=resend_key,
        resend_from=resend_from,
        mailing_list=mailing_list,
        min_rooms=float(os.getenv("MIN_ROOMS", "5.0")),
        max_rent_chf=float(os.getenv("MAX_RENT_CHF", "6500")),
        postcode_prefix=os.getenv("POSTCODE_PREFIX", "80"),
        enable_flatfox=os.getenv("ENABLE_FLATFOX", "true").lower() == "true",
        enable_homegate=os.getenv("ENABLE_HOMEGATE", "true").lower() == "true",
        poll_interval_min=int(os.getenv("POLL_INTERVAL_MIN", "15")),
        poll_jitter_min=int(os.getenv("POLL_JITTER_MIN", "5")),
        google_sheets_id=os.getenv("GOOGLE_SHEETS_ID", ""),
        google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", ""),
        flaresolverr_url=os.getenv("FLARESOLVERR_URL", "http://localhost:8191/v1"),
        flaresolverr_max_timeout_ms=int(os.getenv("FLARESOLVERR_MAX_TIMEOUT_MS", "60000")),
        seen_store_path=os.getenv("SEEN_STORE_PATH", "seen.txt"),
    )
