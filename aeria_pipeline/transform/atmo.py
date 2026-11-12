"""French ATMO air-quality index (arrêté du 10 juillet 2020, in force since 1 January 2021).

Six levels: 1 Bon, 2 Moyen, 3 Dégradé, 4 Mauvais, 5 Très mauvais, 6 Extrêmement mauvais.
Each pollutant gets a sub-index; the index of the day is the worst sub-index and the
pollutant responsible for it is reported.

Aggregation rules per pollutant (concentrations in µg/m³):

* PM2.5, PM10: daily mean
* NO2, O3, SO2: daily maximum of the hourly means

Days are local days (Europe/Paris), as published by the ATMO networks.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

LEVELS = {1: "Bon", 2: "Moyen", 3: "Dégradé", 4: "Mauvais", 5: "Très mauvais", 6: "Extrêmement mauvais"}

# Upper bound (inclusive) of levels 1..5; anything above the last value is level 6.
THRESHOLDS = {
    "pm25": (10, 20, 25, 50, 75),
    "pm10": (20, 40, 50, 100, 150),
    "no2": (40, 90, 120, 230, 340),
    "o3": (50, 100, 130, 240, 380),
    "so2": (100, 200, 350, 500, 750),
}
DAILY_AGG = {"pm25": "mean", "pm10": "mean", "no2": "max", "o3": "max", "so2": "max"}
TZ = "Europe/Paris"
MIN_HOURS = 18  # a daily aggregate needs at least 75 % of the hours


def sub_index(pollutant: str, value: float | np.ndarray | pd.Series) -> np.ndarray:
    """ATMO level (1-6) of a concentration; NaN stays NaN.

    Concentrations are rounded to the unit first, as the official tables are in integers.
    """
    arr = np.asarray(value, dtype=float)
    levels = np.searchsorted(np.asarray(THRESHOLDS[pollutant], dtype=float), np.round(arr), side="left") + 1.0
    return np.where(np.isnan(arr), np.nan, levels)


def daily_index(hourly: pd.DataFrame) -> pd.DataFrame:
    """Daily ATMO index per station from cleaned hourly rows.

    ``hourly`` needs: measured_at (UTC), station_code, pollutant, value (cleaned, NaN = unusable).
    Returns one row per (station, local date) with the aggregated concentration and sub-index of
    every pollutant, the global index, its label and the responsible pollutant.
    """
    df = hourly[hourly["pollutant"].isin(THRESHOLDS) & hourly["value"].notna()].copy()
    if df.empty:
        return pd.DataFrame(columns=["station_code", "date", "atmo_index", "atmo_label", "responsible"])
    df["date"] = df["measured_at"].dt.tz_convert(TZ).dt.date

    parts = []
    for pollutant, how in DAILY_AGG.items():
        sub = df[df["pollutant"] == pollutant]
        if sub.empty:
            continue
        g = sub.groupby(["station_code", "date"])["value"].agg([how, "count"])
        g = g[g["count"] >= MIN_HOURS][how].rename(f"{pollutant}_conc")
        parts.append(g)
    if not parts:
        return pd.DataFrame(columns=["station_code", "date", "atmo_index", "atmo_label", "responsible"])
    day = pd.concat(parts, axis=1).reset_index()

    sub_cols = []
    for pollutant in DAILY_AGG:
        col = f"{pollutant}_conc"
        if col in day:
            day[f"{pollutant}_index"] = sub_index(pollutant, day[col])
            sub_cols.append(f"{pollutant}_index")

    subs = day[sub_cols]
    day["atmo_index"] = subs.max(axis=1)
    # Industrial stations often measure SO2 only: their index rests on a single pollutant and is
    # not comparable with a full index, so the number of pollutants used is kept alongside.
    day["n_pollutants"] = subs.notna().sum(axis=1)
    # Ties: the official rule names all responsible pollutants; we keep the first in table order.
    day["responsible"] = subs.idxmax(axis=1, skipna=True).str.replace("_index", "", regex=False)
    day = day.dropna(subset=["atmo_index"])
    day["atmo_index"] = day["atmo_index"].astype(int)
    day["atmo_label"] = day["atmo_index"].map(LEVELS)
    return day
