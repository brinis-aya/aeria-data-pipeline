import os

import pandas as pd
import pytest

from aeria_pipeline.load import warehouse
from aeria_pipeline.transform import atmo, clean


def test_dim_time_keys_and_dst():
    # 25 Oct 2026: end of summer time in France, local 02:00 happens twice
    ts = pd.Series(pd.date_range("2026-10-24 22:00", periods=4, freq="h", tz="UTC"))
    time, date = warehouse.build_dim_time(ts)
    assert time.time_key.tolist() == [2026102422, 2026102423, 2026102500, 2026102501]
    assert time.hour_local.tolist() == [0, 1, 2, 2]
    assert set(time.date_key) == {20261025}
    d = date.iloc[0]
    assert d.season == "autumn" and d.day_of_week == 7 and d.is_weekend


@pytest.mark.skipif(not os.getenv("DW_TEST_URL"), reason="needs a PostgreSQL (DW_TEST_URL)")
def test_load_is_idempotent():
    ts = pd.date_range("2026-07-01", periods=48, freq="h", tz="UTC")
    raw = pd.DataFrame({
        "measured_at": ts, "station_code": "TEST01", "station_name": "Test", "agency": "ATMO NORMANDIE",
        "zone": "ZAR LE-HAVRE", "area_type": "Urbaine", "influence": "Trafic", "pollutant": "no2",
        "value": 20.0 + (ts.hour % 5), "unit": "µg/m³", "validity": 1, "source": "lcsqa", "valid": True,
    })
    cleaned = clean.clean(raw)
    daily = atmo.daily_index(cleaned)
    with warehouse.connect(os.environ["DW_TEST_URL"]) as conn:
        warehouse.create_schema(conn)
        for _ in range(2):
            run = warehouse.start_run(conn, None, None, "test", len(raw))
            warehouse.load(conn, cleaned, daily, run)
        n, d = conn.execute("""
            SELECT (SELECT count(*) FROM dw.fact_measurement f JOIN dw.dim_station s USING (station_key)
                    WHERE s.station_code = 'TEST01'),
                   (SELECT count(*) FROM dw.fact_daily_index f JOIN dw.dim_station s USING (station_key)
                    WHERE s.station_code = 'TEST01')""").fetchone()
    assert n == 48
    assert d == len(daily) >= 1
