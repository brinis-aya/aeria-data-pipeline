"""Cleaning and quality flagging of hourly air-quality series.

Every input row is kept and receives exactly one quality flag, so the warehouse can always
explain why a value was excluded. Flags are tested in this priority order:

=============  ==============================================================================
``invalid``    flagged invalid by the producer, or no value
``range``      physically implausible (above ``MAX_PLAUSIBLE`` or below ``-NEGATIVE_TOLERANCE``)
``spike``      Hampel filter: far from the rolling median of the surrounding hours
``frozen``     same non-zero value repeated for ``FROZEN_HOURS`` hours or more (stuck sensor)
``clipped``    small negative value (instrument noise around zero), set to 0
``ok``         usable as is
=============  ==============================================================================

``value`` holds the cleaned value: the measurement for ``ok``, 0 for ``clipped``, NaN otherwise.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

KEY = ["source", "station_code", "pollutant"]
FLAGS = ("ok", "clipped", "invalid", "range", "spike", "frozen")
USABLE = ("ok", "clipped")

# µg/m³ - far above anything measured in France, but still below sensor saturation values.
MAX_PLAUSIBLE = {"pm25": 1000.0, "pm10": 2000.0, "no2": 1000.0, "o3": 800.0, "so2": 2000.0}
NEGATIVE_TOLERANCE = 2.0  # analysers report small negatives around zero; below that, it's a fault


@dataclass(frozen=True)
class CleaningConfig:
    hampel_window: int = 7          # hours, centred
    hampel_sigmas: float = 4.0      # threshold in robust standard deviations (1.4826 * MAD)
    hampel_min_jump: float = 25.0   # µg/m³ - ignore "spikes" smaller than this (low-noise series)
    frozen_hours: int = 6
    min_hourly_coverage: float = 0.75  # share of sub-hourly readings needed for an hourly mean


def to_hourly(df: pd.DataFrame, cfg: CleaningConfig = CleaningConfig(), expected_per_hour: int = 4) -> pd.DataFrame:
    """Aggregate sub-hourly readings (IoT sensors every 15 min) to hourly means.

    An hour is only valid if at least ``min_hourly_coverage`` of the expected readings are
    present - the same 75 % rule as the regulatory networks.
    """
    df = df.copy()
    df["hour"] = df["measured_at"].dt.floor("h")
    meta = [c for c in df.columns if c not in ("measured_at", "hour", "value", "valid", "validity")]
    good = df[df["valid"]]
    agg = good.groupby(meta + ["hour"], dropna=False)["value"].agg(["mean", "count"]).reset_index()
    agg["valid"] = agg["count"] >= np.ceil(cfg.min_hourly_coverage * expected_per_hour)
    agg = agg.rename(columns={"hour": "measured_at", "mean": "value"}).drop(columns="count")
    agg["validity"] = np.where(agg["valid"], 1, -1)
    return agg


def _flag_series(s: pd.DataFrame, cfg: CleaningConfig) -> pd.Series:
    """Spike and frozen flags for one (source, station, pollutant) series, sorted by time."""
    # Work on a regular hourly grid so that "7 hours" really means 7 hours, even with gaps.
    values = s.set_index("measured_at")["value_raw"].where(s.set_index("measured_at")["candidate"])
    grid = values.resample("h").mean()

    med = grid.rolling(cfg.hampel_window, center=True, min_periods=3).median()
    mad = (grid - med).abs().rolling(cfg.hampel_window, center=True, min_periods=3).median()
    deviation = (grid - med).abs()
    spike = (deviation > cfg.hampel_sigmas * 1.4826 * mad) & (deviation > cfg.hampel_min_jump)

    # Run lengths of identical consecutive values (NaN breaks a run).
    change = grid.ne(grid.shift()) | grid.isna()
    run_id = change.cumsum()
    run_len = grid.groupby(run_id).transform("size")
    frozen = (run_len >= cfg.frozen_hours) & grid.notna() & (grid.abs() > 1.0)

    flags = pd.Series(np.where(spike, "spike", np.where(frozen, "frozen", "")), index=grid.index)
    return flags.reindex(s["measured_at"]).to_numpy()


def clean(df: pd.DataFrame, cfg: CleaningConfig = CleaningConfig()) -> pd.DataFrame:
    """Return hourly rows with ``value_raw``, cleaned ``value`` and a ``quality`` flag."""
    if df.empty:
        return df.assign(value_raw=pd.Series(dtype=float), quality=pd.Series(dtype=str))
    df = df.sort_values(KEY + ["measured_at"]).drop_duplicates(KEY + ["measured_at"], keep="last")
    df = df.rename(columns={"value": "value_raw"}).reset_index(drop=True)

    upper = df["pollutant"].map(MAX_PLAUSIBLE).fillna(np.inf)
    invalid = ~df["valid"].astype(bool) | df["value_raw"].isna()
    out_of_range = ~invalid & ((df["value_raw"] > upper) | (df["value_raw"] < -NEGATIVE_TOLERANCE))
    df["candidate"] = ~invalid & ~out_of_range

    statistical = np.empty(len(df), dtype=object)
    for _, idx in df.groupby(KEY, sort=False).indices.items():
        statistical[idx] = _flag_series(df.iloc[idx], cfg)

    quality = np.select(
        [invalid, out_of_range, statistical == "spike", statistical == "frozen", df["value_raw"] < 0],
        ["invalid", "range", "spike", "frozen", "clipped"],
        default="ok",
    )
    df["quality"] = quality
    df["value"] = np.where(df["quality"] == "ok", df["value_raw"],
                           np.where(df["quality"] == "clipped", 0.0, np.nan))
    return df.drop(columns=["candidate"])


def quality_report(df: pd.DataFrame) -> pd.DataFrame:
    """Share of each flag per source and pollutant (in %)."""
    counts = df.groupby(["source", "pollutant", "quality"]).size().unstack(fill_value=0)
    for flag in FLAGS:
        if flag not in counts:
            counts[flag] = 0
    counts = counts[list(FLAGS)]
    pct = counts.div(counts.sum(axis=1), axis=0).mul(100).round(2)
    pct.insert(0, "rows", counts.sum(axis=1))
    return pct.reset_index()


def completeness(df: pd.DataFrame) -> pd.DataFrame:
    """Hourly completeness of each series between its first and last timestamp (usable hours only)."""
    rows = []
    for key, s in df.groupby(KEY):
        span = int((s["measured_at"].max() - s["measured_at"].min()) / pd.Timedelta(hours=1)) + 1
        usable = s["quality"].isin(USABLE).sum()
        hours = s["measured_at"].sort_values()
        longest_gap = int((hours.diff().max() or pd.Timedelta(0)) / pd.Timedelta(hours=1)) - 1 if len(hours) > 1 else 0
        rows.append((*key, span, usable, round(100 * usable / span, 2), max(longest_gap, 0)))
    return pd.DataFrame(rows, columns=KEY + ["expected_hours", "usable_hours", "completeness_pct", "longest_gap_h"])
