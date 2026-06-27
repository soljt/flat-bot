from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


class SeenStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._seen: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            log.info("action=store_init path=%s", self._path)
            return
        with open(self._path) as f:
            self._seen = {line.strip() for line in f if line.strip()}
        log.info("action=store_loaded count=%d path=%s", len(self._seen), self._path)

    def contains(self, uid: str) -> bool:
        return uid in self._seen

    def add(self, uid: str) -> None:
        self._seen.add(uid)
        with open(self._path, "a") as f:
            f.write(uid + "\n")
