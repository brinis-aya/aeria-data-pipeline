import numpy as np
import pandas as pd
import pytest

from aeria_pipeline.transform import atmo


@pytest.mark.parametrize("pollutant,value,level", [
    ("pm25", 0, 1), ("pm25", 10, 1), ("pm25", 10.4, 1), ("pm25", 10.6, 2), ("pm25", 25, 3),
    ("pm25", 26, 4), ("pm25", 75, 5), ("pm25", 76, 6),
    ("pm10", 40, 2), ("pm10", 41, 3), ("no2", 91, 3), ("o3", 380, 5), ("o3", 381, 6), ("so2", 99, 1),
])
def test_sub_index_thresholds(pollutant, value, level):
    assert atmo.sub_index(pollutant, value) == level


def test_sub_index_keeps_nan():
    assert np.isnan(atmo.sub_index("no2", [np.nan])[0])


def hourly(pollutant, values, station="S1", day="2026-07-01"):
    # local hours of 1 July (Paris = UTC+2 in summer)
    ts = pd.date_range(f"{day} 00:00", periods=len(values), freq="h", tz=atmo.TZ).tz_convert("UTC")
    return pd.DataFrame({"measured_at": ts, "station_code": station, "pollutant": pollutant, "value": values})


def test_daily_index_uses_mean_for_pm_and_max_for_gases():
    pm = hourly("pm25", [5.0] * 23 + [100.0])    # mean 9.0 -> Bon
    no2 = hourly("no2", [10.0] * 23 + [100.0])  # max 100 -> Dégradé
    r = atmo.daily_index(pd.concat([pm, no2])).iloc[0]
    assert r.pm25_index == 1 and r.no2_index == 3
    assert r.atmo_index == 3 and r.atmo_label == "Dégradé" and r.responsible == "no2"
    assert r.n_pollutants == 2


def test_daily_index_needs_18_hours():
    assert atmo.daily_index(hourly("o3", [60.0] * 17)).empty


def test_days_are_local_days():
    out = atmo.daily_index(hourly("o3", [60.0] * 24))
    assert len(out) == 1 and str(out.iloc[0].date) == "2026-07-01"


def test_unusable_values_are_ignored():
    vals = [20.0] * 24
    vals[3] = np.nan
    assert atmo.daily_index(hourly("pm10", vals)).iloc[0].pm10_conc == 20.0
