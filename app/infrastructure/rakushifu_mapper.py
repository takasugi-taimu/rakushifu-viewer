from datetime import date
from typing import Any, Mapping, Optional

from app.application.errors import InvalidScheduleData
from app.domain.models import Shift, ShiftMonth, Staff, TimeOfDay


def _time(value: Optional[dict]) -> Optional[TimeOfDay]:
    if value is None:
        return None
    return TimeOfDay(hour=int(value["hour"]), minute=int(value["min"]))


def _rests(value) -> tuple:
    return tuple((
        TimeOfDay(int(rest["start_hour"]), int(rest["start_minute"])),
        TimeOfDay(int(rest["end_hour"]), int(rest["end_minute"])),
    ) for rest in (value or []))


def map_schedule(payload: Mapping[str, Any], year: int, month: int) -> ShiftMonth:
    """Translate Rakushifu's JSON schema at the infrastructure boundary."""
    try:
        staff = {
            int(user["id"]): Staff(
                id=int(user["id"]),
                name=user["name"],
                age=user.get("age"),
                rank=user.get("rank_name"),
                birthday=user.get("birthday"),
                employee_code=user.get("employee_code"),
                belonging_store_id=(int(user["belonging_store_id"])
                                    if user.get("belonging_store_id") is not None else None),
            ) for user in payload["users"]
        }
        shifts = tuple(Shift(
            date=date.fromisoformat(item["date"]),
            user_id=int(item["user_id"]),
            store_id=int(item["attending_store_id"]),
            start=_time(item.get("start_time")),
            end=_time(item.get("end_time")),
            rests=_rests(item.get("rest_times")),
        ) for item in payload["shared"])
    except (KeyError, TypeError, ValueError) as error:
        raise InvalidScheduleData("らくしふのシフトデータの形式が不正です") from error
    return ShiftMonth(year=year, month=month, staff=staff, shifts=shifts)
