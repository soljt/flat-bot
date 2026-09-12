from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Profile:
    """
    Searcher-specific email/persona content, loaded from a gitignored TOML file.

    Everything here is personal to whoever is running the bot — the brand shown
    in the subject line, a short description of who is applying, the German
    message the LLM adapts for each landlord, and a static fallback used when the
    LLM call fails.  None of it lives in code so the repo stays reusable.
    """

    subject_prefix: str
    group_description: str
    contact_name: str
    message_template: str
    fallback_message: str
    extra_instructions: str = ""


# Neutral built-in default. Used when no profile.toml is present, and by tests.
# Deliberately generic — it contains no personal data.
_DEFAULT = Profile(
    subject_prefix="Flatbot: ",
    group_description="We are looking for a flat to rent in Zurich.",
    contact_name="",
    message_template=(
        "Guten Tag [Anrede],\n\n"
        "wir interessieren uns sehr für die Wohnung an der [Adresse] und würden "
        "sie gerne besichtigen.\n\n"
        "Wir sind zuverlässige Mieter mit stabiler Anstellung. Wäre es möglich, "
        "einen Besichtigungstermin zu vereinbaren?\n\n"
        "Vielen Dank und freundliche Grüsse,\n"
        "[Ihr Name]\n"
        "[Telefonnummer]"
    ),
    fallback_message=(
        "Guten Tag [Anrede],\n\n"
        "wir sind auf Ihre Wohnung gestossen und interessieren uns sehr dafür. "
        "Sind noch Besichtigungstermine verfügbar?\n\n"
        "Freundliche Grüsse,\n"
        "[Ihr Name]"
    ),
    extra_instructions="",
)


def default_profile() -> Profile:
    """Return the neutral built-in profile (no personal data)."""
    return _DEFAULT


def load_profile(path: str | None) -> Profile:
    """
    Load a Profile from a TOML file. Any field not present in the file falls
    back to the neutral default. If *path* is empty or the file is missing, the
    default profile is returned (with a warning when a path was expected) so the
    bot still runs — it just sends generic emails until a profile is provided.
    """
    if not path:
        return _DEFAULT

    p = Path(path)
    if not p.is_file():
        log.warning(
            "action=profile_missing path=%s — using neutral default (emails will be generic)",
            path,
        )
        return _DEFAULT

    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        log.warning(
            "action=profile_load_failed path=%s — using neutral default",
            path,
            exc_info=True,
        )
        return _DEFAULT

    profile = Profile(
        subject_prefix=data.get("subject_prefix", _DEFAULT.subject_prefix),
        group_description=data.get("group_description", _DEFAULT.group_description),
        contact_name=data.get("contact_name", _DEFAULT.contact_name),
        message_template=data.get("message_template", _DEFAULT.message_template),
        fallback_message=data.get("fallback_message", _DEFAULT.fallback_message),
        extra_instructions=data.get("extra_instructions", ""),
    )
    log.info("action=profile_loaded path=%s", path)
    return profile
