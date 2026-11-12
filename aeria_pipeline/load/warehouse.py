"""Load cleaned data into the PostgreSQL star schema (``sql/schema.sql``).

Loading is idempotent: dimensions are upserted on their natural keys and facts on their
primary key, so re-running the pipeline over the same period updates rows instead of
duplicating them. Facts are bulk-loaded with ``COPY`` into a temporary staging table.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg

from aeria_pipeline.transform.atmo import TZ

log = logging.getLogger(__name__)

SCHEMA_SQL = Path(__file__).resolve().parents[2] / "sql" / "schema.sql"
QUALITY_KEYS = {"ok": 0, "clipped": 1, "invalid": 2, "range": 3, "spike": 4, "frozen": 5}
SEASONS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
           6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
          "septembre", "octobre", "novembre", "décembre"]


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://"))


def create_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.commit()


# ------------------------------------------------------------------ dimension builders

def build_dim_time(ts: pd.Series) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rows of dim_time and dim_date for the distinct UTC hours in ``ts``."""
    hours = pd.DatetimeIndex(pd.to_datetime(ts, utc=True).dt.floor("h").unique()).sort_values()
    local = hours.tz_convert(TZ)
    dates = pd.DatetimeIndex(local.normalize().tz_localize(None))
    time = pd.DataFrame({
        "time_key": hours.strftime("%Y%m%d%H").astype(np.int64),
        "ts_utc": hours,
        "date_key": dates.strftime("%Y%m%d").astype(int),
        "hour_local": local.hour,
        "is_rush_hour": (local.day_of_week < 5) & np.isin(local.hour, [7, 8, 9, 17, 18, 19]),
    })
    d = pd.DatetimeIndex(dates.unique())
    date = pd.DataFrame({
        "date_key": d.strftime("%Y%m%d").astype(int),
        "date_local": d.date,
        "year": d.year, "quarter": d.quarter, "month": d.month,
        "month_name": [MONTHS[m - 1] for m in d.month],
        "iso_week": d.isocalendar().week.to_numpy(),
        "day_of_week": d.day_of_week + 1,
        "is_weekend": d.day_of_week >= 5,
        "season": [SEASONS[m] for m in d.month],
    })
    return time, date


def build_dim_station(df: pd.DataFrame, sensors: pd.DataFrame | None = None) -> pd.DataFrame:
    cols = ["station_code", "station_name", "agency", "zone", "area_type", "influence"]
    st = df[cols + ["source"]].drop_duplicates("station_code", keep="last").copy()
    st["is_reference"] = st["source"] != "aeria-iot"
    st["latitude"] = np.nan
    st["longitude"] = np.nan
    if sensors is not None and not sensors.empty:
        pos = sensors.set_index("sensor_id")[["latitude", "longitude"]]
        st = st.set_index("station_code")
        st.update(pos.rename_axis("station_code"))
        st = st.reset_index()
    return st.drop(columns="source")


# ------------------------------------------------------------------ loaders

def _upsert(conn: psycopg.Connection, table: str, df: pd.DataFrame, key: str, update: bool = True) -> None:
    if df.empty:
        return
    cols = list(df.columns)
    placeholders = ", ".join(["%s"] * len(cols))
    set_clause = ", ".join(f"{c} = COALESCE(EXCLUDED.{c}, {table}.{c})" for c in cols if c != key)
    action = f"DO UPDATE SET {set_clause}" if update and set_clause else "DO NOTHING"
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) ON CONFLICT ({key}) {action}"
    records = [tuple(None if (isinstance(v, float) and np.isnan(v)) else v for v in row)
               for row in df.astype(object).itertuples(index=False, name=None)]
    with conn.cursor() as cur:
        cur.executemany(sql, records)


def _keys(conn: psycopg.Connection, table: str, natural: str, surrogate: str) -> dict:
    return dict(conn.execute(f"SELECT {natural}, {surrogate} FROM {table}").fetchall())


def _copy_upsert(conn: psycopg.Connection, table: str, df: pd.DataFrame, pk: list[str]) -> int:
    """Bulk upsert through a temporary table filled with COPY."""
    cols = list(df.columns)
    stage = f"stage_{table.split('.')[-1]}"
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="\\N")
    buf.seek(0)
    with conn.cursor() as cur:
        cur.execute(f"CREATE TEMP TABLE {stage} (LIKE {table} INCLUDING DEFAULTS) ON COMMIT DROP")
        with cur.copy(f"COPY {stage} ({', '.join(cols)}) FROM STDIN WITH (FORMAT csv, NULL '\\N')") as cp:
            while chunk := buf.read(1 << 20):
                cp.write(chunk)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in pk)
        cur.execute(f"INSERT INTO {table} ({', '.join(cols)}) SELECT {', '.join(cols)} FROM {stage} "
                    f"ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {updates}")
        return cur.rowcount


def start_run(conn: psycopg.Connection, period_start, period_end, sources: str, rows_extracted: int) -> int:
    run_id = conn.execute(
        "INSERT INTO dw.etl_run (period_start, period_end, sources, rows_extracted) VALUES (%s, %s, %s, %s) RETURNING run_id",
        (period_start, period_end, sources, rows_extracted)).fetchone()[0]
    conn.commit()
    return run_id


def finish_run(conn: psycopg.Connection, run_id: int, loaded: int, usable: int, status: str = "success") -> None:
    conn.execute("UPDATE dw.etl_run SET finished_at = now(), rows_loaded = %s, rows_usable = %s, status = %s WHERE run_id = %s",
                 (loaded, usable, status, run_id))
    conn.commit()


def load(conn: psycopg.Connection, clean: pd.DataFrame, daily: pd.DataFrame, run_id: int,
         sensors: pd.DataFrame | None = None) -> dict[str, int]:
    """Load dimensions, hourly facts and daily index facts in one transaction."""
    time, date = build_dim_time(clean["measured_at"])
    if not daily.empty:
        extra = pd.to_datetime(pd.Series(daily["date"].unique())).dt.tz_localize(TZ).dt.tz_convert("UTC")
        _, more = build_dim_time(extra)
        date = pd.concat([date, more]).drop_duplicates("date_key")
    _upsert(conn, "dw.dim_date", date, "date_key", update=False)
    _upsert(conn, "dw.dim_time", time, "time_key", update=False)
    _upsert(conn, "dw.dim_station", build_dim_station(clean, sensors), "station_code")

    station = _keys(conn, "dw.dim_station", "station_code", "station_key")
    pollutant = _keys(conn, "dw.dim_pollutant", "code", "pollutant_key")
    source = _keys(conn, "dw.dim_source", "code", "source_key")

    fact = pd.DataFrame({
        "time_key": clean["measured_at"].dt.strftime("%Y%m%d%H").astype(np.int64),
        "station_key": clean["station_code"].map(station),
        "pollutant_key": clean["pollutant"].map(pollutant),
        "source_key": clean["source"].map(source),
        "quality_key": clean["quality"].map(QUALITY_KEYS),
        "value_raw": clean["value_raw"].round(3),
        "value": clean["value"].round(3),
        "run_id": run_id,
    })
    for col in ("station_key", "pollutant_key", "source_key", "quality_key"):
        fact[col] = fact[col].astype("Int64")
    loaded = _copy_upsert(conn, "dw.fact_measurement", fact,
                          ["time_key", "station_key", "pollutant_key", "source_key"])

    daily_loaded = 0
    if not daily.empty:
        d = pd.DataFrame({
            "date_key": pd.to_datetime(daily["date"]).dt.strftime("%Y%m%d").astype(int),
            "station_key": daily["station_code"].map(station).astype("Int64"),
            "atmo_index": daily["atmo_index"],
            "atmo_label": daily["atmo_label"],
            "responsible_pollutant_key": daily["responsible"].map(pollutant).astype("Int64"),
            "n_pollutants": daily["n_pollutants"].astype(int),
            "run_id": run_id,
        })
        for p in ("pm25", "pm10", "no2", "o3", "so2"):
            d[f"{p}_conc"] = daily[f"{p}_conc"].round(2) if f"{p}_conc" in daily else np.nan
            d[f"{p}_index"] = daily[f"{p}_index"].astype("Int64") if f"{p}_index" in daily else pd.NA
        daily_loaded = _copy_upsert(conn, "dw.fact_daily_index", d, ["date_key", "station_key"])

    conn.commit()
    log.info("Loaded %d hourly facts and %d daily index rows (run %d)", loaded, daily_loaded, run_id)
    return {"hourly": loaded, "daily": daily_loaded}
