"""Exchange session calendar for futures (futures-first, docs/futures.md).

Deterministic and in the exchange's own zone, so daylight-saving changes are
handled by ``zoneinfo`` rather than by fixed UTC hours. Hermes may reason about
sessions; only this decides whether the engine may open a position.

The ``cme_globex`` template: the week opens Sunday 17:00 Central, each day
trades until 16:00 Central, pauses for the 16:00-17:00 maintenance window and
reopens at 17:00 for the next trading day; Friday 16:00 closes the week. A
session that opens at 17:00 belongs to the next calendar day's trading day.

Holidays are where exchanges differ year to year (full closures, early closes
at 12:00 or 12:15, shifted settlements), and CME's holiday page could not be
read from this environment. So ATLAS does not try to trade them: every US
exchange holiday, plus the day after Thanksgiving, Christmas Eve and New
Year's Eve, is a no-trade trading day, worked out by the rules below. The
operator can add more days (``extra_no_trade``). Being flat on a day the
market is open costs a missed trade; being in on a day it closes early can
break a prop rule.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from zoneinfo import ZoneInfo

TEMPLATES = {
    # tz, daily close, daily reopen (the maintenance window lies between), week opens Sunday at reopen,
    # week closes Friday at close.
    "cme_globex": {"tz": "America/Chicago", "close": dt.time(16, 0), "reopen": dt.time(17, 0)},
}


@dataclass(frozen=True)
class SessionState:
    open: bool
    phase: str  # rth | eth | maintenance | weekend | no_trade_day
    trading_day: dt.date
    local: str  # exchange-zone wall time, for the journal
    closes_at: dt.datetime | None  # UTC end of the current trading day's session (None when closed)
    reason: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["trading_day"] = self.trading_day.isoformat()
        d["closes_at"] = self.closes_at.isoformat() if self.closes_at else None
        return d


@dataclass
class SessionCalendar:
    extra_no_trade: frozenset[dt.date] = field(default_factory=frozenset)

    def status(self, template: str, rth: tuple[str, str], now: dt.datetime) -> SessionState:
        if now.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (UTC everywhere)")
        t = TEMPLATES[template]
        tz = ZoneInfo(t["tz"])
        local = now.astimezone(tz)
        wall = local.strftime("%a %H:%M %Z")
        clock = local.time()
        # Trading day: the session opening at the reopen time belongs to the next day.
        day = local.date() + dt.timedelta(days=1) if clock >= t["reopen"] else local.date()
        wd = local.weekday()
        if wd == 5 or (wd == 6 and clock < t["reopen"]) or (wd == 4 and clock >= t["close"]):
            return SessionState(False, "weekend", day, wall, None, "market_closed_weekend")
        if t["close"] <= clock < t["reopen"]:
            return SessionState(False, "maintenance", day, wall, None, "market_closed_maintenance")
        if day.weekday() >= 5:  # Friday-evening edge cases never reach here; belt and braces
            return SessionState(False, "weekend", day, wall, None, "market_closed_weekend")
        if day in no_trade_days(day.year) or day in self.extra_no_trade:
            return SessionState(False, "no_trade_day", day, wall, None, "exchange_holiday")
        closes = dt.datetime.combine(day, t["close"], tz).astimezone(dt.timezone.utc)
        start, end = (dt.time.fromisoformat(x) for x in rth)
        phase = "rth" if local.date() == day and start <= clock < end else "eth"
        return SessionState(True, phase, day, wall, closes)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    d = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> dt.date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    lv = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lv) // 451
    month = (h + lv - 7 * m + 114) // 31
    day = (h + lv - 7 * m + 114) % 31 + 1
    return dt.date(year, month, day)


def _observed(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=1) if d.weekday() == 5 else d + dt.timedelta(days=1) if d.weekday() == 6 else d


@lru_cache(maxsize=16)
def no_trade_days(year: int) -> frozenset[dt.date]:
    """US exchange holidays (observed) plus the usual early-close days, for ``year``."""
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    days = {
        _observed(dt.date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Presidents' Day
        _easter(year) - dt.timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed(dt.date(year, 6, 19)),  # Juneteenth
        _observed(dt.date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        thanksgiving,
        thanksgiving + dt.timedelta(days=1),  # early close
        _observed(dt.date(year, 12, 25)),
        dt.date(year, 12, 24),  # early close
        dt.date(year, 12, 31),  # early close
        _observed(dt.date(year + 1, 1, 1)),  # New Year's Day observed on a Friday falls in this year
    }
    return frozenset(d for d in days if d.year == year and d.weekday() < 5)
