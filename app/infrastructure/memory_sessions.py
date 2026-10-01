import secrets
import time
from threading import RLock
from typing import Dict, Optional, Tuple

from app.application.session import ActiveSession


class MemorySessionStore:
    """Opaque, expiring app sessions. State stays in this server process only."""

    def __init__(self, lifetime_seconds: int = 3600, max_sessions: int = 500):
        self.lifetime_seconds = lifetime_seconds
        self.max_sessions = max_sessions
        self._items: Dict[str, Tuple[float, ActiveSession]] = {}
        self._lock = RLock()

    def _remove_expired(self) -> None:
        now = time.monotonic()
        expired = [token for token, (expires, _) in self._items.items()
                   if expires <= now]
        for token in expired:
            _, value = self._items.pop(token)
            value.close()

    def create(self, value: ActiveSession) -> str:
        with self._lock:
            self._remove_expired()
            if len(self._items) >= self.max_sessions:
                oldest = min(self._items, key=lambda token: self._items[token][0])
                _, removed = self._items.pop(oldest)
                removed.close()
            token = secrets.token_urlsafe(32)
            self._items[token] = (time.monotonic() + self.lifetime_seconds, value)
            return token

    def get(self, token: str) -> Optional[ActiveSession]:
        with self._lock:
            self._remove_expired()
            item = self._items.get(token)
            return item[1] if item else None

    def delete(self, token: str) -> None:
        with self._lock:
            item = self._items.pop(token, None)
            if item:
                item[1].close()
