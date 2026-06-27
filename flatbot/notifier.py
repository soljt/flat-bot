from __future__ import annotations

import html
import logging
import re
from abc import ABC, abstractmethod

import httpx

log = logging.getLogger(__name__)


def _html_to_text(html_body: str) -> str:
    """Strip HTML tags and unescape entities to produce a plain-text fallback."""
    # Remove style/script blocks entirely
    text = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", html_body, flags=re.DOTALL | re.IGNORECASE)
    # Replace block-level breaks with newlines before stripping tags
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|h[1-6]|hr|tr)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<hr\s*/?>", "\n---\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Unescape HTML entities
    text = html.unescape(text)
    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class Notifier(ABC):
    @abstractmethod
    def send(self, subject: str, body: str, recipients: list[str]) -> None:
        ...


class ResendNotifier(Notifier):
    _URL = "https://api.resend.com/emails"

    def __init__(self, api_key: str, from_email: str) -> None:
        self._api_key = api_key
        self._from_email = from_email

    def send(self, subject: str, body: str, recipients: list[str]) -> None:
        resp = httpx.post(
            self._URL,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": self._from_email,
                "to": recipients,
                "subject": subject,
                "html": body,
                "text": _html_to_text(body),
            },
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Resend error {resp.status_code}: {resp.text[:300]}"
            )
        log.info(
            "action=email_sent recipients=%d subject=%r",
            len(recipients),
            subject[:80],
        )
