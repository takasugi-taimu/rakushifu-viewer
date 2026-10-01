from dataclasses import dataclass
from datetime import date
from typing import Mapping, Optional, Tuple


@dataclass(frozen=True, order=True)
class TimeOfDay:
    hour: int
    minute: int

    def __post_init__(self):
        # The upstream schedule uses 24:00 and 25:00 for next-day hours.
        if not 0 <= self.hour <= 47 or not 0 <= self.minute <= 59:
            raise ValueError("Invalid time of day")

    @property
    def minutes(self) -> int:
        return self.hour * 60 + self.minute

    def __str__(self) -> str:
        return f"{self.hour}:{self.minute:02d}"


@dataclass(frozen=True)
class Staff:
    id: int
    name: str
    age: Optional[int] = None
    rank: Optional[str] = None
    birthday: Optional[str] = None
    employee_code: Optional[str] = None
    belonging_store_id: Optional[int] = None


@dataclass(frozen=True)
class Shift:
    date: date
    user_id: int
    store_id: int
    start: Optional[TimeOfDay]
    end: Optional[TimeOfDay]
    rests: Tuple[Tuple[TimeOfDay, TimeOfDay], ...] = ()

    @property
    def duration_minutes(self) -> int:
        if self.start is None or self.end is None:
            return 0
        duration = self.end.minutes - self.start.minutes
        return duration if duration >= 0 else duration + 24 * 60

    @property
    def rest_intervals_minutes(self) -> Tuple[Tuple[int, int], ...]:
        if self.start is None or self.end is None:
            return ()
        shift_start = self.start.minutes
        shift_end = shift_start + self.duration_minutes
        intervals = []
        for rest_start, rest_end in self.rests:
            start = rest_start.minutes
            end = rest_end.minutes
            if end < start:
                end += 1440
            # Upstream may express a post-midnight break as 00:xx or 24:xx.
            candidates = [(start + offset, end + offset)
                          for offset in (-1440, 0, 1440)]
            start, end = max(candidates, key=lambda interval:
                             max(0, min(shift_end, interval[1])
                                 - max(shift_start, interval[0])))
            start, end = max(shift_start, start), min(shift_end, end)
            if start < end:
                intervals.append((start, end))
        merged = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        return tuple(merged)

    @property
    def rest_minutes(self) -> int:
        return sum(end - start for start, end in self.rest_intervals_minutes)

    @property
    def work_minutes(self) -> int:
        return self.duration_minutes - self.rest_minutes


@dataclass(frozen=True)
class ShiftMonth:
    year: int
    month: int
    staff: Mapping[int, Staff]
    shifts: Tuple[Shift, ...]


@dataclass(frozen=True)
class Viewer:
    account_id: str
    staff_id: int
    store_id: int
    genre_id: int
