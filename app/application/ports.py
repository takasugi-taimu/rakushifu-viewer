from typing import TYPE_CHECKING, Optional, Protocol

from app.domain.models import ShiftMonth, Viewer

if TYPE_CHECKING:
    from .session import ActiveSession


class ScheduleConnection(Protocol):
    viewer: Viewer

    def fetch(self, year: int, month: int, store_id: int,
              genre_id: int) -> ShiftMonth:
        ...

    def close(self) -> None:
        ...


class Authenticator(Protocol):
    def login(self, employee_code: str, password: str) -> ScheduleConnection:
        ...


class SessionStore(Protocol):
    def create(self, value: "ActiveSession") -> str:
        ...

    def get(self, token: str) -> Optional["ActiveSession"]:
        ...

    def delete(self, token: str) -> None:
        ...
