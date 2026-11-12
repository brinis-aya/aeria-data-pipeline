import numpy as np
import pandas as pd

from aeria_pipeline.transform import clean


def series(values, pollutant="pm25", start="2026-01-01", source="lcsqa", station="S1"):
    n = len(values)
    return pd.DataFrame({
        "measured_at": pd.date_range(start, periods=n, freq="h", tz="UTC"),
        "station_code": station, "station_name": "x", "agency": "a", "zone": "z",
        "area_type": "Urbaine", "influence": "Fond", "pollutant": pollutant,
        "value": values, "unit": "µg/m³", "validity": 1, "source": source,
        "valid": [not pd.isna(v) for v in values],
    })


def noisy(n=48, seed=0):
    rng = np.random.default_rng(seed)
    return list(np.round(10 + 3 * np.sin(np.arange(n) / 4) + rng.normal(0, 0.8, n), 1))


def test_clean_series_is_all_ok():
    out = clean.clean(series(noisy()))
    assert (out.quality == "ok").all()
    assert out.value.equals(out.value_raw)


def test_spike_is_flagged_and_removed():
    values = noisy()
    values[20] = 180.0
    out = clean.clean(series(values))
    assert out.loc[20, "quality"] == "spike"
    assert np.isnan(out.loc[20, "value"])
    assert (out.drop(index=20).quality == "ok").all()


def test_small_jump_is_not_a_spike():
    values = noisy()
    values[20] += 15  # large in sigmas but below hampel_min_jump
    assert (clean.clean(series(values)).quality == "ok").all()


def test_frozen_sensor():
    values = noisy()
    values[10:18] = [42.0] * 8
    out = clean.clean(series(values))
    assert (out.loc[10:17, "quality"] == "frozen").all()
    assert out.loc[9, "quality"] == "ok"


def test_repeated_zero_is_not_frozen():
    values = noisy()
    values[10:18] = [0.0] * 8
    assert not (clean.clean(series(values)).quality == "frozen").any()


def test_negative_handling_and_range():
    values = noisy()
    values[5], values[6], values[7] = -0.7, -50.0, 5000.0
    out = clean.clean(series(values))
    assert out.loc[5, "quality"] == "clipped" and out.loc[5, "value"] == 0.0
    assert out.loc[6, "quality"] == "range"
    assert out.loc[7, "quality"] == "range"


def test_invalid_and_duplicates():
    df = series(noisy(10))
    df.loc[3, "valid"] = False
    df = pd.concat([df, df.iloc[[4]]])
    out = clean.clean(df)
    assert len(out) == 10
    assert out.loc[3, "quality"] == "invalid"


def test_series_are_flagged_independently():
    a = series(noisy(), station="A")
    b = series([42.0] * 48, station="B")  # a frozen station must not contaminate A
    out = clean.clean(pd.concat([a, b]))
    assert (out[out.station_code == "A"].quality == "ok").all()
    assert (out[out.station_code == "B"].quality == "frozen").all()


def test_to_hourly_applies_75_percent_rule():
    ts = pd.date_range("2026-01-01", periods=8, freq="15min", tz="UTC")
    df = pd.DataFrame({"measured_at": ts, "station_code": "LH-001", "pollutant": "pm25", "source": "aeria-iot",
                       "value": [10, 12, 14, 16, 20, np.nan, np.nan, np.nan], "validity": 1})
    df["valid"] = df.value.notna()
    out = clean.to_hourly(df).sort_values("measured_at")
    assert out.value.tolist()[0] == 13.0
    assert out.valid.tolist() == [True, False]


def test_quality_report_sums_to_100():
    values = noisy()
    values[20] = 180.0
    rep = clean.quality_report(clean.clean(series(values)))
    assert abs(rep[list(clean.FLAGS)].sum(axis=1).iloc[0] - 100) < 0.01
