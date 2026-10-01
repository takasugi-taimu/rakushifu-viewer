from datetime import date, datetime, timedelta, timezone
import time
from typing import Dict, List

from app.domain.models import Shift, ShiftMonth, Viewer
from app.domain.services import estimate_pay, overlaps

from .errors import SessionExpired, StaffNotFound
from .ports import Authenticator, SessionStore
from .session import ActiveSession


def _store_shifts(month: ShiftMonth, viewer: Viewer) -> List[Shift]:
    return [shift for shift in month.shifts
            if shift.store_id == viewer.store_id and shift.start is not None]


JAPAN_TIME = timezone(timedelta(hours=9))


def _scheduled_staff_ids(month: ShiftMonth, viewer: Viewer,
                         year: int, month_number: int) -> set[int]:
    return {
        shift.user_id for shift in _store_shifts(month, viewer)
        if shift.date.year == year and shift.date.month == month_number
    }


class ShiftUseCases:
    def __init__(self, authenticator: Authenticator, sessions: SessionStore,
                 cache_seconds: int = 120, max_cached_months: int = 3):
        self.authenticator = authenticator
        self.sessions = sessions
        self.cache_seconds = cache_seconds
        self.max_cached_months = max_cached_months

    def login(self, employee_code: str, password: str, today: date = None) -> str:
        today = today if today is not None else datetime.now(JAPAN_TIME).date()
        connection = self.authenticator.login(employee_code, password)
        try:
            viewer = connection.viewer
            month = connection.fetch(today.year, today.month,
                                     viewer.store_id, viewer.genre_id)
            active = ActiveSession(viewer=viewer, connection=connection)
            active.cache[(today.year, today.month)] = (
                time.monotonic() + self.cache_seconds, month)
            return self.sessions.create(active)
        except Exception:
            connection.close()
            raise

    def logout(self, token: str) -> None:
        self.sessions.delete(token)

    def _active(self, token: str) -> ActiveSession:
        active = self.sessions.get(token) if token else None
        if active is None:
            raise SessionExpired("ログインが必要です")
        return active

    def authenticated(self, token: str) -> bool:
        return self.sessions.get(token) is not None if token else False

    def _month(self, active: ActiveSession, year: int, month: int) -> ShiftMonth:
        key = (year, month)
        with active.lock:
            cached = active.cache.get(key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            result = active.connection.fetch(
                year, month, active.viewer.store_id, active.viewer.genre_id)
            now = time.monotonic()
            for old_key, (expires, _) in list(active.cache.items()):
                if expires <= now:
                    del active.cache[old_key]
            if key not in active.cache and len(active.cache) >= self.max_cached_months:
                oldest = min(active.cache, key=lambda item: active.cache[item][0])
                del active.cache[oldest]
            active.cache[key] = (now + self.cache_seconds, result)
            return result

    def day(self, target: date, token: str) -> dict:
        active = self._active(token)
        viewer = active.viewer
        month = self._month(active, target.year, target.month)
        shifts = [shift for shift in _store_shifts(month, viewer)
                  if shift.date == target]
        my_shift = next((shift for shift in shifts if shift.user_id == viewer.staff_id), None)
        workers = []
        for shift in shifts:
            staff = month.staff.get(shift.user_id)
            is_me = shift.user_id == viewer.staff_id
            workers.append({
                "user_id": shift.user_id,
                "name": staff.name if staff else "不明",
                "age": staff.age if staff else None,
                "rank": staff.rank if staff else None,
                "time": f"{shift.start} - {shift.end}" if shift.end else "未定",
                "rest_times": [f"{start} - {end}" for start, end in shift.rests],
                "is_me": is_me,
                "is_overlapping": bool(my_shift and not is_me and overlaps(my_shift, shift)),
                "is_same_end": bool(my_shift and not is_me and my_shift.end
                                    and shift.end and my_shift.end == shift.end),
                "_start": shift.start,
            })
        workers.sort(key=lambda worker: (
            0 if worker["is_me"] else (1 if worker["is_overlapping"] else 2),
            worker["_start"].minutes,
        ))
        for worker in workers:
            del worker["_start"]
        return {
            "date": target.isoformat(),
            "count": len(workers),
            "workers": workers,
            "has_my_shift": my_shift is not None,
        }

    def calendar(self, year: int, month_number: int, token: str) -> Dict[str, dict]:
        active = self._active(token)
        viewer = active.viewer
        month = self._month(active, year, month_number)
        days: Dict[str, dict] = {}
        for shift in _store_shifts(month, viewer):
            if shift.date.year != year or shift.date.month != month_number:
                continue
            key = shift.date.isoformat()
            day = days.setdefault(key, {
                "has_me": False,
                "my_shift_time": None,
                "total_count": 0,
            })
            day["total_count"] += 1
            if shift.user_id == viewer.staff_id:
                day["has_me"] = True
                day["my_shift_time"] = str(shift.start)
                day["my_shift_end_time"] = str(shift.end) if shift.end else None
        return days

    def staff(self, user_id: int, year: int, month_number: int,
              token: str) -> dict:
        active = self._active(token)
        viewer = active.viewer
        month = self._month(active, year, month_number)
        staff = month.staff.get(user_id)
        scheduled_ids = _scheduled_staff_ids(month, viewer, year, month_number)
        if (staff is None or
                (staff.id != viewer.staff_id
                 and staff.belonging_store_id != viewer.store_id
                 and staff.id not in scheduled_ids)):
            raise StaffNotFound("ユーザーが見つかりません")
        schedules = []
        total_minutes = 0
        weekdays = ("月", "火", "水", "木", "金", "土", "日")
        for shift in _store_shifts(month, viewer):
            if (shift.user_id != user_id or shift.date.year != year
                    or shift.date.month != month_number):
                continue
            duration = shift.work_minutes
            total_minutes += duration
            schedules.append({
                "date": shift.date.isoformat(),
                "date_display": f"{shift.date.month}月{shift.date.day}日",
                "day_of_week": weekdays[shift.date.weekday()],
                "time": f"{shift.start} - {shift.end}",
                "duration_minutes": duration,
                "rest_minutes": shift.rest_minutes,
                "rest_times": [f"{start} - {end}" for start, end in shift.rests],
            })
        schedules.sort(key=lambda item: item["date"])
        return {
            "user_id": user_id,
            "name": staff.name,
            "birthday": staff.birthday,
            "age": staff.age,
            "rank": staff.rank,
            "employee_code": staff.employee_code,
            "schedules": schedules,
            "total_work_minutes": total_minutes,
        }

    def search_staff(self, query: str, year: int, month_number: int,
                     token: str) -> list:
        active = self._active(token)
        month = self._month(active, year, month_number)
        # The schedule response can contain users from other stores. Only show
        # people belonging to this store or working a shift here this month.
        scheduled_ids = _scheduled_staff_ids(
            month, active.viewer, year, month_number)
        needle = query.strip().casefold()
        results = []
        for staff in month.staff.values():
            if staff.id == active.viewer.staff_id:
                continue
            if (staff.belonging_store_id != active.viewer.store_id
                    and staff.id not in scheduled_ids):
                continue
            if needle not in staff.name.casefold() and needle not in (staff.employee_code or "").casefold():
                continue
            results.append({
                "user_id": staff.id,
                "name": staff.name,
                "employee_code": staff.employee_code,
                "rank": staff.rank,
                "has_shift": staff.id in scheduled_ids,
            })
        results.sort(key=lambda item: (item["name"].casefold(), item["user_id"]))
        return results[:100]

    def my_pay(self, year: int, month_number: int, token: str,
               hourly_wage, night_bonus_percent) -> dict:
        active = self._active(token)
        month = self._month(active, year, month_number)
        shifts = [shift for shift in _store_shifts(month, active.viewer)
                  if shift.user_id == active.viewer.staff_id
                  and shift.date.year == year and shift.date.month == month_number]
        result = estimate_pay(shifts, hourly_wage, night_bonus_percent)
        result["user_id"] = active.viewer.staff_id
        result["shift_count"] = len(shifts)
        return result
