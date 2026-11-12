"""Command line entry point.

    python -m aeria_pipeline.cli run --start 2025-11-01 --end 2026-11-11
    python -m aeria_pipeline.cli init-db
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import pandas as pd

from aeria_pipeline.extract import lcsqa, sensors
from aeria_pipeline.load import warehouse
from aeria_pipeline.transform import atmo, clean

log = logging.getLogger("aeria_pipeline")

DEFAULT_DW = os.getenv("DW_URL", "postgresql://aeria:aeria@127.0.0.1:5440/aeria")
DEFAULT_IOT = os.getenv("IOT_DB_URL", "postgresql+psycopg://aeria:aeria@127.0.0.1:5440/aeria")


def extract(args) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    frames, sensor_meta = [], None
    if "lcsqa" in args.sources:
        flt = lcsqa.LcsqaFilter(agencies=tuple(args.agency))
        df = lcsqa.fetch_range(args.start, args.end, Path(args.cache), flt)
        log.info("lcsqa: %d hourly rows, %d stations", len(df), df["station_code"].nunique() if len(df) else 0)
        frames.append(df)
    if "aeria-iot" in args.sources:
        start = datetime.combine(args.start, dtime.min, timezone.utc)
        end = datetime.combine(args.end + timedelta(days=1), dtime.min, timezone.utc)
        try:
            raw, sensor_meta = sensors.from_database(args.iot_db, start, end)
            hourly = clean.to_hourly(raw)
            log.info("aeria-iot: %d readings -> %d hourly rows", len(raw), len(hourly))
            frames.append(hourly)
        except Exception as exc:  # the IoT platform is optional: the open data alone is enough
            log.warning("aeria-iot source skipped: %s", exc)
    frames = [f for f in frames if not f.empty]
    return (pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()), sensor_meta


def run(args) -> None:
    t0 = time.perf_counter()
    raw, sensor_meta = extract(args)
    if raw.empty:
        raise SystemExit("Nothing extracted - check the period and the sources")
    t_extract = time.perf_counter()

    cleaned = clean.clean(raw)
    daily = atmo.daily_index(cleaned)
    t_transform = time.perf_counter()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = clean.quality_report(cleaned)
    report.to_csv(out / "quality_report.csv", index=False)
    clean.completeness(cleaned).to_csv(out / "completeness.csv", index=False)
    daily.to_csv(out / "daily_atmo_index.csv", index=False)
    log.info("Quality report:\n%s", report.to_string(index=False))

    if args.no_load:
        log.info("--no-load: skipping the warehouse")
        return
    with warehouse.connect(args.dw) as conn:
        warehouse.create_schema(conn)
        run_id = warehouse.start_run(conn, args.start, args.end, ",".join(args.sources), len(raw))
        try:
            counts = warehouse.load(conn, cleaned, daily, run_id, sensor_meta)
        except Exception:
            conn.rollback()
            warehouse.finish_run(conn, run_id, 0, 0, "failed")
            raise
        usable = int(cleaned["quality"].isin(clean.USABLE).sum())
        warehouse.finish_run(conn, run_id, counts["hourly"], usable)
    t_load = time.perf_counter()
    log.info("Done: extract %.1fs, transform %.1fs, load %.1fs", t_extract - t0, t_transform - t_extract, t_load - t_transform)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="aeria-pipeline", description="Aeria open-data ETL")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="extract, clean, compute the ATMO index and load the warehouse")
    r.add_argument("--start", type=date.fromisoformat, default=date.today() - timedelta(days=7))
    r.add_argument("--end", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    r.add_argument("--sources", nargs="+", default=["lcsqa", "aeria-iot"], choices=["lcsqa", "aeria-iot"])
    r.add_argument("--agency", nargs="+", default=["ATMO NORMANDIE"])
    r.add_argument("--cache", default=os.getenv("AERIA_CACHE", ".cache/lcsqa"))
    r.add_argument("--out", default="out")
    r.add_argument("--dw", default=DEFAULT_DW, help="warehouse DSN")
    r.add_argument("--iot-db", default=DEFAULT_IOT, help="SQLAlchemy URL of the aeria-iot-sensors database")
    r.add_argument("--no-load", action="store_true", help="stop after the transform step")
    r.set_defaults(func=run)

    i = sub.add_parser("init-db", help="create the dw schema")
    i.add_argument("--dw", default=DEFAULT_DW)
    i.set_defaults(func=lambda a: warehouse.create_schema(warehouse.connect(a.dw)))

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
