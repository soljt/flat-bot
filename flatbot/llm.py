from __future__ import annotations

import html as html_module
import logging
import re

import anthropic

from .adapters.base import Listing

log = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_SUBJECT_PREFIX = "Spikeboys Flatbot: "


def generate_email(
    listing: Listing, api_key: str, sheets_url: str | None = None
) -> tuple[str, str]:
    """
    Return (subject, html_body).

    Subject is always deterministic (rooms / postcode / price).
    The HTML body is a deterministic template; the LLM is called only to
    write the suggested landlord message, which it returns as plain text.
    Pass *sheets_url* to include a coordination note with a link to the
    match-tracking Google Sheet before the suggested landlord message.
    """
    subject = fallback_subject(listing)
    landlord_msg = _get_landlord_message(listing, api_key)
    body = _render_email_body(listing, landlord_msg, sheets_url)
    return subject, body


# ── Public fallbacks (used by tests and by generate_email error path) ─────────

def fallback_subject(listing: Listing) -> str:
    price = _price_str(listing)
    return f"{_SUBJECT_PREFIX}{listing.rooms or '?'}R {listing.postcode or 'Zürich'} — {price}"


def fallback_body(listing: Listing, sheets_url: str | None = None) -> str:
    return _render_email_body(listing, _fallback_landlord_message(), sheets_url)


# ── Email body template ───────────────────────────────────────────────────────

def _render_email_body(
    listing: Listing, landlord_msg_html: str, sheets_url: str | None = None
) -> str:
    """Build the full HTML email body from a deterministic template + landlord message."""
    price_str = _price_str(listing)
    platform_cap = listing.platform.capitalize()

    flag_items: list[str] = []
    if listing.price_is_teaser:
        flag_items.append("Teaser/starting-from price — actual rent may be higher")
    if listing.price_on_request:
        flag_items.append("Price on request — confirm before applying")
    if listing.no_wg_clause:
        flag_items.append("Possible 'no shared flat' clause — check before applying")

    flags_html = ""
    if flag_items:
        items = "".join(f"<li><strong>⚠️ {f}</strong></li>" for f in flag_items)
        flags_html = f"\n<ul>{items}</ul>"

    sheets_html = ""
    if sheets_url:
        sheets_html = (
            f'\n<p><strong>Match log:</strong> '
            f'<a href="{sheets_url}">Open Google Sheet</a> ({sheets_url})</p>'
        )

    coordination_html = ""
    if sheets_url:
        coordination_html = (
            f'\n<p><strong>Before you apply:</strong> mark the '
            f'<strong>"Human Sent Message"</strong> column in our '
            f'<a href="{sheets_url}">match-tracking Google Sheet</a> ({sheets_url}) '
            f'before reaching out — so only one of us contacts the landlord.</p>'
        )

    return f"""\
<p><a href="{listing.url}"><strong>View listing on {platform_cap}</strong></a></p>{sheets_html}
{flags_html}
<ul>
  <li><strong>Title:</strong> {listing.title}</li>
  <li><strong>Rooms:</strong> {listing.rooms}</li>
  <li><strong>Rent:</strong> {price_str}</li>
  <li><strong>Address:</strong> {listing.address or 'not specified'}</li>
  <li><strong>Available from:</strong> {listing.available_from or 'not specified'}</li>
</ul>

<hr>{coordination_html}
<p><strong>Suggested message to landlord (adapt as needed):</strong></p>

{landlord_msg_html}"""


# ── Landlord message ──────────────────────────────────────────────────────────

def _get_landlord_message(listing: Listing, api_key: str) -> str:
    """Try LLM for the landlord message; fall back to the template on any failure."""
    try:
        return _llm_landlord_message(listing, api_key)
    except Exception:
        log.warning(
            "platform=%s action=llm_landlord_failed id=%s — using template",
            listing.platform,
            listing.id,
            exc_info=True,
        )
        return _fallback_landlord_message()


def _llm_landlord_message(listing: Listing, api_key: str) -> str:
    """
    Ask the LLM to write the suggested landlord message as plain text.
    Converts the result to HTML paragraphs.
    """
    price_str = _price_str(listing)
    prompt = f"""Write a suggested German-language message from our group to the landlord for this flat listing.

We are "Spikeboys" — 4 friends, 25–30 years old, all ETH graduates now working in the tech industry, looking for a flat in Zurich together.

LISTING:
Title: {listing.title}
Address: {listing.address or 'not specified'}
Rooms: {listing.rooms}
Rent: {price_str}
Available from: {listing.available_from or 'not specified'}
Description excerpt: {listing.description[:400]}

Base the message closely on this template. Adapt the greeting if the landlord's name appears in the listing. Naturally weave in one brief, specific detail about the flat if something stands out — otherwise keep it close to the template.

---
Guten Tag [Anrede],

Ich bin gerade auf dieses Objekt gestossen und es erfüllt genau unsere Anforderungen. Wir sind 4 befreundete junge Erwachsene (25–30 Jahre alt), alle ETH Absolventen die jetzt in der Tech-Branche tätig sind, und suchen jetzt gemeinsam nach einer permanenten Bleibe in Zürich. Unser vollständiges Dossier finden Sie im Anhang.
Sind noch Besichtigungstermine verfügbar? Wir könnten die Wohnung direkt bestätigen.

Beste Grüsse,
[Ihr Name]
---

Respond with ONLY the message text. Plain text, blank lines between paragraphs (greeting / body / sign-off). No HTML, no explanation, nothing else."""

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    plain_text = msg.content[0].text.strip()
    return _plain_text_to_html(plain_text)


def _fallback_landlord_message() -> str:
    return (
        "<p>Guten Tag [Anrede],</p>\n\n"
        "<p>Ich bin gerade auf dieses Objekt gestossen und es erfüllt genau unsere Anforderungen. "
        "Wir sind 4 befreundete junge Erwachsene (25–30 Jahre alt), alle ETH Absolventen die jetzt "
        "in der Tech-Branche tätig sind, und suchen jetzt gemeinsam nach einer permanenten Bleibe "
        "in Zürich. Unser vollständiges Dossier finden Sie im Anhang.<br>\n"
        "Sind noch Besichtigungstermine verfügbar? Wir könnten die Wohnung direkt bestätigen.</p>\n\n"
        "<p>Beste Grüsse,<br>\n[Ihr Name]</p>"
    )


def _plain_text_to_html(text: str) -> str:
    """
    Convert plain text with blank-line paragraph separators to HTML.
    Each paragraph becomes a <p> block; single newlines within a paragraph
    become <br> so the line structure is preserved.
    """
    paragraphs = re.split(r"\n\s*\n", text.strip())
    parts: list[str] = []
    for para in paragraphs:
        lines = para.strip().splitlines()
        escaped = "<br>\n".join(html_module.escape(ln) for ln in lines)
        parts.append(f"<p>{escaped}</p>")
    return "\n\n".join(parts)


# ── Helpers ───────────────────────────────────────────────────────────────────

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
