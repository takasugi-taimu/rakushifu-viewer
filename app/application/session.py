from dataclasses import dataclass, field
from threading import RLock
from typing import Dict, Tuple

from app.domain.models import ShiftMonth, Viewer

from .ports import ScheduleConnection


@dataclass
class ActiveSession:
    viewer: Viewer
    connection: ScheduleConnection
    cache: Dict[Tuple[int, int], Tuple[float, ShiftMonth]] = field(default_factory=dict)
    lock: RLock = field(default_factory=RLock)

    def close(self) -> None:
        with self.lock:
            self.cache.clear()
            self.connection.close()
