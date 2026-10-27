"""MySQL/MariaDB storage for the kWh history and the daily reports.

Three tables hold everything:

    samples  every reading of the meter counters (default: every 15 minutes)
    daily    one row per day per meter, the source of the reports
    meta     small key/value store (how far the daily rollup has run)

The meter counters are cumulative, so a day's consumption is simply the last
counter of that day minus the last counter before the day started. A day is
flagged "complete" only when both readings are close enough to the midnight
edges; otherwise it is reported as partial instead of inventing a number.
"""

from __future__ import annotations

import configparser
import os
from datetime import date, datetime, time, timedelta

import pymysql
import pymysql.cursors

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("KWH_CONFIG", os.path.join(BASE_DIR, "config.ini"))

_FALLBACK_CONFIG = {
    "database": {"host": "127.0.0.1", "port": "3306", "user": "energi", "password": "", "database": "energy"},
    "dashboard": {"host": "127.0.0.1", "port": "5000"},
    "history": {"sample_minutes": "15"},
}


class StorageError(RuntimeError):
    """Raised when the database is unreachable or a query fails."""


def _load_config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    if os.path.exists(CONFIG_PATH):
        parser.read(CONFIG_PATH, encoding="utf-8")
    else:
        parser.read_dict(_FALLBACK_CONFIG)
    return parser


_config = _load_config()


def _setting(env: str, section: str, option: str, fallback: str) -> str:
    return os.environ.get(env) or _config.get(section, option, fallback=fallback)


DB_HOST = _setting("KWH_DB_HOST", "database", "host", "127.0.0.1")
DB_PORT = int(_setting("KWH_DB_PORT", "database", "port", "3306"))
DB_USER = _setting("KWH_DB_USER", "database", "user", "energi")
DB_PASSWORD = _setting("KWH_DB_PASSWORD", "database", "password", "")
DB_NAME = _setting("KWH_DB_NAME", "database", "database", "energy")

DASHBOARD_HOST = _setting("KWH_DASHBOARD_HOST", "dashboard", "host", "127.0.0.1")
DASHBOARD_PORT = int(_setting("KWH_DASHBOARD_PORT", "dashboard", "port", "5000"))
SAMPLE_MINUTES = int(_setting("KWH_SAMPLE_MINUTES", "history", "sample_minutes", "15"))

# A reporting day ends at this hour, not at midnight: the tariff cycle runs
# 22:00 -> 22:00 (LWBP from 22:00 to 18:00, WBP from 18:00 to 22:00), so one
# LWBP+WBP cycle fits inside a single row. The row is labelled with the date
# the day closes on: day "17 Oct" covers 16 Oct 22:00 -> 17 Oct 22:00.
DAY_END_HOUR = int(_setting("KWH_DAY_END_HOUR", "history", "day_end_hour", "22"))

# A day is trusted only when the readings that bracket it sit this close to the
# day edges. Allow roughly two sampling intervals, with a floor so that sampling
# every minute does not turn a couple of missed polls into a "partial" day.
COMPLETE_TOLERANCE = max(timedelta(minutes=2 * SAMPLE_MINUTES), timedelta(minutes=15))


def _window(day: date) -> tuple[datetime, datetime]:
    """Start and end of the reporting day labelled `day`.

    With day_end_hour = 0 the day is simply the calendar date. With a closing
    hour such as 22 the day is the 24 hours ending at that hour, labelled with
    the date it closes on, so one row holds a complete LWBP + WBP cycle.
    """
    if DAY_END_HOUR == 0:
        start = datetime.combine(day, time.min)
        return start, start + timedelta(days=1)
    end = datetime.combine(day, time(hour=DAY_END_HOUR))
    return end - timedelta(days=1), end


def _cycle_day(moment: datetime) -> date:
    """The reporting day a timestamp belongs to."""
    if DAY_END_HOUR == 0:
        return moment.date()
    day = moment.date()
    return day if moment.time() < time(hour=DAY_END_HOUR) else day + timedelta(days=1)


def day_window(day: date) -> tuple[datetime, datetime]:
    """Public helper: start and end of the reporting day labelled `day`."""
    return _window(day)

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS samples (
        ts DATETIME NOT NULL,
        meter_id VARCHAR(8) NOT NULL,
        kwh DOUBLE NULL,
        kwh_t1 DOUBLE NULL,
        kwh_t2 DOUBLE NULL,
        PRIMARY KEY (ts, meter_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS daily (
        day DATE NOT NULL,
        meter_id VARCHAR(8) NOT NULL,
        kwh DOUBLE NULL,
        kwh_t1 DOUBLE NULL,
        kwh_t2 DOUBLE NULL,
        complete TINYINT(1) NOT NULL DEFAULT 1,
        PRIMARY KEY (day, meter_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
        k VARCHAR(64) NOT NULL,
        v VARCHAR(255) NULL,
        PRIMARY KEY (k)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)


def connect() -> pymysql.connections.Connection:
    """Open a connection. Raises StorageError with a readable message."""
    try:
        return pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=5,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except pymysql.MySQLError as exc:
        raise StorageError(
            f"Tidak bisa terhubung ke database {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}: {exc}"
        ) from exc


def init_schema() -> None:
    """Create the tables if they do not exist yet."""
    with connect() as conn:
        with conn.cursor() as cur:
            for statement in _SCHEMA:
                cur.execute(statement)


def status() -> dict:
    """Small health summary for the dashboard."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION() AS version")
            version = cur.fetchone()["version"]
            cur.execute("SELECT COUNT(*) AS n FROM samples")
            samples = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM daily")
            days = cur.fetchone()["n"]
    return {
        "host": DB_HOST,
        "port": DB_PORT,
        "database": DB_NAME,
        "user": DB_USER,
        "version": version,
        "samples": samples,
        "days": days,
        "sampleMinutes": SAMPLE_MINUTES,
        "dayEndHour": DAY_END_HOUR,
    }


def write_sample(ts: datetime, meter: dict) -> None:
    """Store one reading of a meter's cumulative counters."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO samples (ts, meter_id, kwh, kwh_t1, kwh_t2) VALUES (%s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE kwh = VALUES(kwh), kwh_t1 = VALUES(kwh_t1), kwh_t2 = VALUES(kwh_t2)",
                (ts.replace(microsecond=0), meter["id"], meter["kwh"], meter["kwh_t1"], meter["kwh_t2"]),
            )


def _meta_get(cur, key: str) -> str | None:
    cur.execute("SELECT v FROM meta WHERE k = %s", (key,))
    row = cur.fetchone()
    return row["v"] if row else None


def _meta_set(cur, key: str, value: str) -> None:
    cur.execute(
        "INSERT INTO meta (k, v) VALUES (%s, %s) ON DUPLICATE KEY UPDATE v = VALUES(v)",
        (key, value),
    )


def _difference(end: float | None, start: float | None) -> float | None:
    """Day consumption from two cumulative counters; None if unusable."""
    if end is None or start is None or end < start:
        return None
    return end - start


def _readings(cur, meter_id: str, first_day: date, last_day: date) -> list[dict]:
    """Readings covering the whole cycle of first_day through last_day."""
    cur.execute(
        "SELECT ts, kwh, kwh_t1, kwh_t2 FROM samples "
        "WHERE meter_id = %s AND ts >= %s AND ts < %s ORDER BY ts",
        (meter_id, _window(first_day)[0], _window(last_day)[1]),
    )
    return cur.fetchall()


def _sync_boundary_setting(cur) -> None:
    """Rebuild the daily table when the day-end hour changes."""
    stored = _meta_get(cur, "day_end_hour")
    if stored == str(DAY_END_HOUR):
        return
    cur.execute("DELETE FROM daily")
    cur.execute("DELETE FROM meta WHERE k = 'last_finalized_day'")
    _meta_set(cur, "day_end_hour", str(DAY_END_HOUR))


def _write_days(cur, meter_id: str, first_day: date, last_day: date, readings: list[dict]) -> None:
    def last_at_or_before(moment: datetime) -> dict | None:
        # A sample sitting exactly on the boundary is the day's opening reading.
        found = None
        for row in readings:
            if row["ts"] <= moment:
                found = row
            else:
                break
        return found

    def last_within(day_start: datetime, day_end: datetime) -> dict | None:
        found = None
        for row in readings:
            if day_start <= row["ts"] < day_end:
                found = row
            elif row["ts"] >= day_end:
                break
        return found

    day = first_day
    while day <= last_day:
        day_start, day_end = _window(day)
        start = last_at_or_before(day_start)
        end = last_within(day_start, day_end)

        complete = 1
        if start is None or end is None:
            complete = 0
        elif day_start - start["ts"] > COMPLETE_TOLERANCE or day_end - end["ts"] > COMPLETE_TOLERANCE:
            complete = 0

        cur.execute(
            "INSERT INTO daily (day, meter_id, kwh, kwh_t1, kwh_t2, complete) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE kwh = VALUES(kwh), kwh_t1 = VALUES(kwh_t1), "
            "kwh_t2 = VALUES(kwh_t2), complete = VALUES(complete)",
            (
                day,
                meter_id,
                _difference(end["kwh"] if end else None, start["kwh"] if start else None),
                _difference(end["kwh_t1"] if end else None, start["kwh_t1"] if start else None),
                _difference(end["kwh_t2"] if end else None, start["kwh_t2"] if start else None),
                complete,
            ),
        )
        day += timedelta(days=1)


def finalize_days(meter_ids: list[str], today: date | None = None) -> int:
    """Roll samples up into the daily table for every day that has closed.

    A day closes at DAY_END_HOUR, so "today" here means the cycle that is still
    running. Returns how many days were finalised; safe to call repeatedly.
    """
    running_day = today or _cycle_day(datetime.now())
    last_closed = running_day - timedelta(days=1)
    written = 0

    with connect() as conn:
        with conn.cursor() as cur:
            _sync_boundary_setting(cur)

            marker = _meta_get(cur, "last_finalized_day")
            if marker is None:
                cur.execute("SELECT MIN(ts) AS first_ts FROM samples")
                first_ts = cur.fetchone()["first_ts"]
                # Start one day before the first sample so that day becomes the
                # baseline for the first full day of data.
                start = (_cycle_day(first_ts) - timedelta(days=1)) if first_ts else last_closed
                _meta_set(cur, "last_finalized_day", start.isoformat())
                return 0

            first_day = date.fromisoformat(marker) + timedelta(days=1)
            if first_day > last_closed:
                return 0

            for meter_id in meter_ids:
                readings = _readings(cur, meter_id, first_day, last_closed)
                _write_days(cur, meter_id, first_day, last_closed, readings)
            written = (last_closed - first_day).days + 1
            _meta_set(cur, "last_finalized_day", last_closed.isoformat())

    return written


def daily_rows(first_day: date, last_day: date) -> list[dict]:
    """Daily rows (per meter) for the report window."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT day, meter_id, kwh, kwh_t1, kwh_t2, complete FROM daily "
                "WHERE day >= %s AND day <= %s ORDER BY day, meter_id",
                (first_day, last_day),
            )
            return cur.fetchall()


def readings_between(first: datetime, last: datetime, meter_ids: list[str]) -> list[dict]:
    """Cumulative counters in [first, last), ordered per meter by time."""
    if not meter_ids:
        return []
    placeholders = ",".join(["%s"] * len(meter_ids))
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT meter_id, ts, kwh FROM samples "
                f"WHERE ts >= %s AND ts < %s AND meter_id IN ({placeholders}) "
                "ORDER BY meter_id, ts",
                (first, last, *meter_ids),
            )
            return cur.fetchall()
