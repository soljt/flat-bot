from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

log = logging.getLogger(__name__)


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
                "text": body,
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
