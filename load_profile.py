"""Hourly load profile and monthly heatmap.

The meter only stores cumulative energy, so the average power over an interval
is derived from two consecutive samples:

    kW = (kWh_end - kWh_start) / hours

No extra column is needed and the historical 15-minute data keeps working next
to the newer one-minute data.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

import storage

# An interval longer than this is treated as a gap: the missing consumption is
# not spread over time we never measured.
MAX_INTERVAL = timedelta(minutes=30)

# An hour counts as complete when at least 90% of it is covered by readings.
HOUR_COMPLETE_SECONDS = 0.9 * 3600


def _empty_day() -> list[dict]:
    return [{"kwh": 0.0, "seconds": 0.0} for _ in range(24)]


def _distribute(buckets: list[dict], day_start, day_end, t0, t1, delta: float) -> None:
    """Split one interval's energy across the hours it overlaps."""
    span = (t1 - t0).total_seconds()
    if span <= 0:
        return
    cursor = max(t0, day_start)
    end = min(t1, day_end)
    while cursor < end:
        index = int((cursor - day_start).total_seconds() // 3600)
        hour_end = day_start + timedelta(hours=index + 1)
        segment_end = min(end, hour_end)
        overlap = (segment_end - cursor).total_seconds()
        if overlap > 0 and 0 <= index < 24:
            buckets[index]["kwh"] += delta * overlap / span
            buckets[index]["seconds"] += overlap
        cursor = segment_end


def _meter_buckets(rows: list[dict], day_start, day_end) -> list[dict]:
    buckets = _empty_day()
    previous = None
    for row in rows:
        if (previous is not None
                and previous["kwh"] is not None
                and row["kwh"] is not None
                and row["kwh"] >= previous["kwh"]):
            t0, t1 = previous["ts"], row["ts"]
            gap = t1 - t0
            if timedelta(0) < gap <= MAX_INTERVAL:
                _distribute(buckets, day_start, day_end, t0, t1, row["kwh"] - previous["kwh"])
        previous = row
    return buckets


def _status(seconds: float) -> str:
    if seconds <= 0:
        return "missing"
    return "complete" if seconds >= HOUR_COMPLETE_SECONDS else "partial"


def hourly_profile(day: date, meters: list[dict], selection: str = "all") -> dict:
    """Average power and energy for every hour of one reporting day."""
    ids = [meter["id"] for meter in meters]
    if selection not in ids:
        selection = "all"

    day_start, day_end = storage.day_window(day)
    # Read a little on both sides so the interval that starts before the day and
    # the one that closes it at the boundary can still be measured.
    rows = storage.readings_between(day_start - MAX_INTERVAL, day_end + MAX_INTERVAL, ids)

    grouped: dict[str, list] = {meter_id: [] for meter_id in ids}
    for row in rows:
        grouped.setdefault(row["meter_id"], []).append(row)

    per_meter = {
        meter_id: _meter_buckets(grouped.get(meter_id, []), day_start, day_end)
        for meter_id in ids
    }

    hours = []
    for index in range(24):
        if selection == "all":
            kwh = sum(per_meter[meter_id][index]["kwh"] for meter_id in ids)
            seconds = max((per_meter[meter_id][index]["seconds"] for meter_id in ids), default=0.0)
            powers = []
            for meter_id in ids:
                item = per_meter[meter_id][index]
                if item["seconds"] > 0:
                    powers.append(item["kwh"] / (item["seconds"] / 3600))
            kw = sum(powers) if powers else None
            reporting = len(powers)
        else:
            item = per_meter[selection][index]
            kwh = item["kwh"]
            seconds = item["seconds"]
            kw = (kwh / (seconds / 3600)) if seconds > 0 else None
            reporting = 1 if seconds > 0 else 0

        hours.append({
            "hour": index,
            "label": f"{index:02d}:00",
            "kw": kw,
            "kwh": kwh if seconds > 0 else None,
            "coveredMinutes": round(seconds / 60, 1),
            "status": _status(seconds),
            "metersReporting": reporting,
        })

    ranked = sorted((hour for hour in hours if hour["kw"] is not None),
                    key=lambda hour: hour["kw"], reverse=True)
    total_kwh = sum(hour["kwh"] for hour in hours if hour["kwh"] is not None)

    return {
        "date": day.isoformat(),
        "dayEndHour": storage.DAY_END_HOUR,
        "selection": selection,
        "meters": [{"id": meter["id"], "name": meter["name"]} for meter in meters],
        "hours": hours,
        "busiest": [{"hour": hour["hour"], "label": hour["label"], "kw": hour["kw"]}
                    for hour in ranked[:3]],
        "peakKw": ranked[0]["kw"] if ranked else None,
        "peakHour": ranked[0]["hour"] if ranked else None,
        "totalKwh": total_kwh,
        "hasData": bool(ranked),
    }


def monthly_heatmap(year: int, month: int, meters: list[dict], selection: str = "all") -> dict:
    """One row per day of the month, 24 hourly kW values each."""
    days_in_month = calendar.monthrange(year, month)[1]
    days = []
    max_kw = 0.0

    for number in range(1, days_in_month + 1):
        profile = hourly_profile(date(year, month, number), meters, selection)
        values = [hour["kw"] for hour in profile["hours"]]
        for value in values:
            if value is not None and value > max_kw:
                max_kw = value
        days.append({
            "day": number,
            "hours": values,
            "totalKwh": profile["totalKwh"],
            "hasData": profile["hasData"],
        })

    return {
        "year": year,
        "month": month,
        "selection": selection,
        "maxKw": max_kw,
        "days": days,
    }
