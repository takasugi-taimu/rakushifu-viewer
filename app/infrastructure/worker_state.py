"""JSON boundary for data passed to a session Durable Object."""

import json
from datetime import date

from app.domain.models import Shift, ShiftMonth, Staff, TimeOfDay, Viewer


def viewer_to_dict(viewer):
    return {
        "account_id": viewer.account_id,
        "staff_id": viewer.staff_id,
        "store_id": viewer.store_id,
        "genre_id": viewer.genre_id,
    }


def viewer_from_dict(value):
    return Viewer(**value)


def month_to_json(month):
    return json.dumps({
        "year": month.year,
        "month": month.month,
        "staff": [vars(person) for person in month.staff.values()],
        "shifts": [{
            "date": shift.date.isoformat(),
            "user_id": shift.user_id,
            "store_id": shift.store_id,
            "start": [shift.start.hour, shift.start.minute] if shift.start else None,
            "end": [shift.end.hour, shift.end.minute] if shift.end else None,
            "rests": [[[start.hour, start.minute], [end.hour, end.minute]]
                      for start, end in shift.rests],
        } for shift in month.shifts],
    }, ensure_ascii=False, separators=(",", ":"))


def month_from_json(value):
    data = json.loads(value)
    staff = {person["id"]: Staff(**person) for person in data["staff"]}
    shifts = []
    for item in data["shifts"]:
        shifts.append(Shift(
            date.fromisoformat(item["date"]), item["user_id"], item["store_id"],
            TimeOfDay(*item["start"]) if item["start"] else None,
            TimeOfDay(*item["end"]) if item["end"] else None,
            tuple((TimeOfDay(*start), TimeOfDay(*end))
                  for start, end in item["rests"]),
        ))
    return ShiftMonth(data["year"], data["month"], staff, tuple(shifts))
