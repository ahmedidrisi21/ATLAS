"""Central bank policy rates for carry (BIS WS_CBPOL, daily).

The BIS publishes each central bank's policy rate as a daily series at::

    https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.{AREAS}?format=csv

Carry for a pair is the base currency's rate minus the quote currency's, in
percent a year. A long position earns it each rollover night and a short pays
it. Values are taken as of the previous UTC day, so a decision never sees a
rate change announced later the same day.
"""

from __future__ import annotations

import datetime as dt
import urllib.request
from pathlib import Path

import pandas as pd

BIS_URL = "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.{areas}?startPeriod={start}&endPeriod={end}&format=csv"
AREA = {"USD": "US", "EUR": "XM", "GBP": "GB", "JPY": "JP", "AUD": "AU", "NZD": "NZ", "CAD": "CA", "CHF": "CH"}


def download(path: Path, start: dt.date, end: dt.date) -> Path:
    url = BIS_URL.format(areas="+".join(AREA.values()), start=start, end=end)
    with urllib.request.urlopen(url, timeout=60) as resp:
        body = resp.read()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def load(path: Path) -> pd.DataFrame:
    """Daily policy rates in percent, one column per currency, forward-filled over every calendar day."""
    raw = pd.read_csv(path, usecols=["REF_AREA", "TIME_PERIOD", "OBS_VALUE"])
    wide = raw.pivot_table(index="TIME_PERIOD", columns="REF_AREA", values="OBS_VALUE")
    wide.index = pd.DatetimeIndex(pd.to_datetime(wide.index), tz="UTC")
    by_ccy = wide.rename(columns={a: c for c, a in AREA.items()})
    return by_ccy.reindex(pd.date_range(by_ccy.index[0], by_ccy.index[-1], freq="D")).ffill()


def carry(rates: pd.DataFrame, symbol: str, times: pd.DatetimeIndex) -> pd.Series:
    """Base minus quote policy rate (% a year) as of the day before each time."""
    base, quote = symbol[:3].upper(), symbol[3:6].upper()
    diff = rates[base] - rates[quote]
    days = times.tz_convert("UTC").normalize() - pd.Timedelta(days=1)
    return pd.Series(diff.reindex(days).to_numpy(), index=times)
