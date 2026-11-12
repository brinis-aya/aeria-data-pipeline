"""Extract measurements from the Aeria IoT platform (module 1, ``aeria-iot-sensors``).

Two access paths are supported:

* ``from_database`` reads the operational PostgreSQL store directly (fast, used in batch);
* ``from_api`` pages through ``GET /api/measurements/export?after=`` (no DB credentials needed).

Both return the same tidy long format as the LCSQA extractor so that the transform step does
not need to know where the data came from.
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
import requests
from sqlalchemy import create_engine, text

log = logging.getLogger(__name__)

SENSOR_POLLUTANTS = ("pm25", "pm10", "no2", "o3")

SENSOR_SQL = text("""
    SELECT s.id AS sensor_id, s.name, s.latitude, s.longitude, s.environment
    FROM sensor s ORDER BY s.id
""")

MEASUREMENT_SQL = text("""
    SELECT sensor_id, measured_at, pm25, pm10, no2, o3
    FROM measurement
    WHERE measured_at >= :start AND measured_at < :end
    ORDER BY sensor_id, measured_at
""")


def to_long(wide: pd.DataFrame, sensors: pd.DataFrame) -> pd.DataFrame:
    """Wide sensor rows (one column per pollutant) -> one row per (sensor, pollutant, time)."""
    if wide.empty:
        return pd.DataFrame(columns=["measured_at", "station_code", "pollutant", "value"])
    long = wide.melt(id_vars=["sensor_id", "measured_at"], value_vars=list(SENSOR_POLLUTANTS),
                     var_name="pollutant", value_name="value")
    long = long.merge(sensors, on="sensor_id", how="left")
    out = pd.DataFrame({
        "measured_at": pd.to_datetime(long["measured_at"], utc=True),
        "station_code": long["sensor_id"],
        "station_name": long.get("name"),
        "agency": "AERIA",
        "zone": "LE HAVRE",
        "area_type": long.get("environment"),
        "influence": "Capteur basse coût",
        "pollutant": long["pollutant"],
        "value": pd.to_numeric(long["value"], errors="coerce"),
        "unit": "µg/m³",
        "validity": 1,
        "source": "aeria-iot",
    })
    out["valid"] = out["value"].notna()
    return out.dropna(subset=["value"]).reset_index(drop=True)


def from_database(db_url: str, start: datetime, end: datetime) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (long measurements, sensor metadata) for ``start <= measured_at < end``."""
    engine = create_engine(db_url)
    with engine.connect() as conn:
        sensors = pd.read_sql(SENSOR_SQL, conn)
        wide = pd.read_sql(MEASUREMENT_SQL, conn, params={"start": start, "end": end})
    log.info("aeria-iot: %d raw rows from %d sensors", len(wide), len(sensors))
    return to_long(wide, sensors), sensors


def from_api(base_url: str, after: datetime | None = None, page_limit: int = 1000) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Page through the export endpoint of the ingestion service."""
    http = requests.Session()
    sensors = pd.DataFrame(http.get(f"{base_url}/api/sensors", timeout=30).json())
    sensors = sensors.rename(columns={"id": "sensor_id"})
    rows, cursor = [], (after.isoformat() if after else "1970-01-01T00:00:00Z")
    for _ in range(page_limit):
        page = http.get(f"{base_url}/api/measurements/export",
                        params={"after": cursor, "limit": 20000}, timeout=60).json()
        if not page:
            break
        rows.extend(page)
        # The watermark is strict (> after). Sensors publish on the same quarter-hours, so the
        # cursor steps back one second and duplicates are removed in the transform step.
        last = pd.Timestamp(page[-1]["measuredAt"]) - pd.Timedelta(seconds=1)
        if len(page) < 20000:
            break
        cursor = last.isoformat()
    wide = pd.DataFrame(rows).rename(columns={"sensorId": "sensor_id", "measuredAt": "measured_at"})
    return to_long(wide, sensors), sensors
