from __future__ import annotations

import logging

import anthropic

from .adapters.base import Listing

log = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"


def generate_email(listing: Listing, api_key: str) -> tuple[str, str]:
    """Return (subject, body) for a new listing match. Falls back to template on failure."""
    try:
        return _llm_generate(listing, api_key)
    except Exception:
        log.warning(
            "platform=%s action=llm_failed id=%s — using template fallback",
            listing.platform,
            listing.id,
            exc_info=True,
        )
        return fallback_subject(listing), fallback_body(listing)


def _llm_generate(listing: Listing, api_key: str) -> tuple[str, str]:
    price_str = _price_str(listing)

    flags: list[str] = []
    if listing.price_is_teaser:
        flags.append("TEASER PRICE — actual rent may be higher than listed; verify before applying")
    if listing.price_on_request:
        flags.append("PRICE ON REQUEST — confirm rent with landlord before applying")
    if listing.no_wg_clause:
        flags.append("POSSIBLE NO-SHARED-FLAT CLAUSE — description may disqualify group tenants; read carefully")

    flag_block = "\n".join(f"- {f}" for f in flags) if flags else "None"

    prompt = f"""Generate a notification email for a new flat listing. We are a group of 4 people flat-hunting in Zurich. We apply manually — this email alerts the group so they can act quickly.

LISTING:
Platform: {listing.platform}
URL: {listing.url}
Title: {listing.title}
Rooms: {listing.rooms}
Rent: {price_str}
Postcode: {listing.postcode}
Address: {listing.address or 'not specified'}
Available from: {listing.available_from or 'not specified'}
Description (excerpt): {listing.description[:600]}

FLAGS:
{flag_block}

Respond with ONLY these two sections, nothing before or after:

SUBJECT: <one line — include rooms, Zurich postcode, and price>
BODY:
<body — include the URL, key facts (rooms/rent/address/availability), any flags clearly called out, then a suggested generic German-language message to the landlord the group can adapt. The suggested message must contain zero personal details — no names, no dossier contents. End with [Ihr Name] as a placeholder for the sender's name.>"""

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()

    subject = ""
    body_lines: list[str] = []
    in_body = False

    for line in text.splitlines():
        if not in_body and line.startswith("SUBJECT:"):
            subject = line[len("SUBJECT:"):].strip()
        elif line.strip() == "BODY:":
            in_body = True
        elif in_body:
            body_lines.append(line)

    subject = subject.strip()
    body = "\n".join(body_lines).strip()

    if not subject or not body:
        log.warning(
            "platform=%s action=llm_parse_failed id=%s — falling back to template",
            listing.platform,
            listing.id,
        )
        return fallback_subject(listing), fallback_body(listing)

    return subject, body


def fallback_subject(listing: Listing) -> str:
    price = _price_str(listing)
    return f"[FlatBot] {listing.rooms or '?'}R {listing.postcode or 'Zürich'} — {price}"


def fallback_body(listing: Listing) -> str:
    flags: list[str] = []
    if listing.price_is_teaser:
        flags.append("⚠️  Teaser/starting-from price — actual rent may be higher")
    if listing.price_on_request:
        flags.append("⚠️  Price on request — confirm before applying")
    if listing.no_wg_clause:
        flags.append("⚠️  Possible 'no shared flat' clause — check before applying")

    flag_block = ("\n" + "\n".join(flags) + "\n") if flags else ""

    rent_detail = _price_str(listing)

    return f"""New listing match:

{listing.url}
{flag_block}
Details
───────
Rooms:      {listing.rooms}
Rent:       {rent_detail}
Postcode:   {listing.postcode}
Address:    {listing.address or 'not specified'}
Available:  {listing.available_from or 'not specified'}
Platform:   {listing.platform}

───────────────────────────────────────────────
Suggested message to landlord (adapt as needed)
───────────────────────────────────────────────

Sehr geehrte Damen und Herren

Wir haben Ihr Inserat auf {listing.platform.capitalize()} entdeckt und interessieren uns sehr für die Wohnung.

Wir sind eine Gruppe von 4 Personen mit vollständigem Dossier (Betreibungsregisterauszüge, Lohnausweise usw.), das wir Ihnen auf Wunsch gerne umgehend zustellen.

Für Fragen oder einen Besichtigungstermin stehen wir jederzeit zur Verfügung.

Freundliche Grüsse
[Ihr Name]
"""


def _price_str(listing: Listing) -> str:
    if listing.price_on_request:
        return "Preis auf Anfrage"
    if listing.price_is_teaser and listing.price_chf:
        return f"ab CHF {listing.price_chf:,.0f}/Mo"
    if listing.price_is_teaser:
        return "ab CHF (Betrag nicht angegeben)"
    if listing.price_chf:
        return f"CHF {listing.price_chf:,.0f}/Mo"
    return "Preis unbekannt"
