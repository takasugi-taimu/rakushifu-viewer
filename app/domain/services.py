from decimal import Decimal, ROUND_HALF_UP

from .models import Shift


def overlaps(left: Shift, right: Shift) -> bool:
    """Check whether two shifts on the same date share working time."""
    if left.date != right.date or not left.start or not right.start:
        return False
    left_end = left.start.minutes + left.duration_minutes
    right_end = right.start.minutes + right.duration_minutes
    return left.start.minutes < right_end and right.start.minutes < left_end


def estimate_pay(shifts, hourly_wage: Decimal,
                 night_bonus_percent: Decimal) -> dict:
    """Estimate pay from scheduled work and the upstream break intervals."""
    scheduled = worked = night = 0
    for shift in shifts:
        if shift.start is None or shift.end is None:
            continue
        duration = shift.duration_minutes
        scheduled += duration
        shift_night = 0
        start = shift.start.minutes
        end = start + duration
        # Include the previous day's night period for shifts starting at 00:00.
        for day in range(start // 1440 - 1, end // 1440 + 1):
            night_start, night_end = day * 1440 - 120, day * 1440 + 300
            segment_start, segment_end = max(start, night_start), min(end, night_end)
            if segment_start >= segment_end:
                continue
            shift_night += segment_end - segment_start
            for rest_start, rest_end in shift.rest_intervals_minutes:
                shift_night -= max(0, min(segment_end, rest_end)
                                   - max(segment_start, rest_start))
        worked += shift.work_minutes
        night += shift_night
    pay = (hourly_wage * Decimal(worked) / 60
           + hourly_wage * night_bonus_percent * Decimal(night) / 6000)
    return {
        "scheduled_minutes": scheduled,
        "break_minutes": scheduled - worked,
        "worked_minutes": worked,
        "night_minutes": night,
        "estimated_yen": int(pay.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
    }
