"""Extract hourly regulatory measurements published by LCSQA / Geod'air on data.gouv.fr.

Dataset: « Données temps réel de mesure des concentrations de polluants atmosphériques
réglementés » — one CSV per day (``FR_E2_YYYY-MM-DD.csv``, ~12 MB, all of France).
Licence: Licence Ouverte / Etalab 2.0.

To keep disk usage low, each day is downloaded, filtered (agencies, pollutants) and only the
filtered rows are cached, gzip-compressed.
"""
from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

BASE_URL = (
    "https://files.data.gouv.fr/ineris/lcsqa/"
    "concentrations-de-polluants-atmospheriques-reglementes/temps-reel/{year}/FR_E2_{day}.csv"
)

# Source column -> our column
COLUMNS = {
    "Date de début": "start",
    "Organisme": "agency",
    "code zas": "zone_code",
    "Zas": "zone",
    "code site": "station_code",
    "nom site": "station_name",
    "type d'implantation": "area_type",
    "Polluant": "pollutant",
    "type d'influence": "influence",
    "type de valeur": "value_type",
    "valeur": "value",
    "unité de mesure": "unit",
    "code qualité": "quality_code",
    "validité": "validity",
}

# LCSQA names -> Aeria pollutant codes
POLLUTANTS = {"NO2": "no2", "PM10": "pm10", "PM2.5": "pm25", "O3": "o3", "SO2": "so2"}
VALID_CODES = (1, 4)


@dataclass(frozen=True)
class LcsqaFilter:
    agencies: tuple[str, ...] = ("ATMO NORMANDIE",)
    pollutants: tuple[str, ...] = tuple(POLLUTANTS)


def parse(raw: bytes | str, flt: LcsqaFilter = LcsqaFilter()) -> pd.DataFrame:
    """Parse one daily CSV and return tidy rows: one (station, pollutant, hour) per line."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig")
    df = pd.read_csv(io.StringIO(raw), sep=";", dtype=str, usecols=list(COLUMNS))
    df = df.rename(columns=COLUMNS)
    df = df[df["agency"].isin(flt.agencies) & df["pollutant"].isin(flt.pollutants)]
    df = df[df["value_type"].str.startswith("moyenne horaire", na=False)]
    if df.empty:
        return df.assign(measured_at=pd.Series(dtype="datetime64[ns, UTC]"))

    out = pd.DataFrame({
        # Timestamps are published in UTC (« heure TU » in the dataset documentation).
        "measured_at": pd.to_datetime(df["start"], format="%Y/%m/%d %H:%M:%S", utc=True),
        "station_code": df["station_code"],
        "station_name": df["station_name"],
        "agency": df["agency"],
        "zone": df["zone"],
        "area_type": df["area_type"],
        "influence": df["influence"],
        "pollutant": df["pollutant"].map(POLLUTANTS),
        "value": pd.to_numeric(df["value"].str.replace(",", "."), errors="coerce"),
        "unit": df["unit"].str.replace("µg-m3", "µg/m³", regex=False),
        # Geod'air validity code: 1 = valid, 4 = pre-validated (automatic checks only),
        # -1 = invalid / missing. Real-time data is never fully validated, so 1 and 4 are kept.
        "validity": pd.to_numeric(df["validity"], errors="coerce").fillna(-1).astype(int),
        "source": "lcsqa",
    })
    out["valid"] = out["validity"].isin(VALID_CODES) & out["value"].notna()
    return out.reset_index(drop=True)


def _cache_path(cache_dir: Path, day: date) -> Path:
    return cache_dir / f"FR_E2_{day.isoformat()}.csv.gz"


def _read_cache(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"station_code": str})
    df["measured_at"] = pd.to_datetime(df["measured_at"], utc=True)
    if "validity" not in df:  # caches written before the validity code was kept
        df["validity"] = df["value"].notna().map({True: 1, False: -1})
    df["valid"] = df["validity"].isin(VALID_CODES) & df["value"].notna()
    return df


def fetch_day(day: date, cache_dir: Path, flt: LcsqaFilter = LcsqaFilter(),
              session: requests.Session | None = None, retries: int = 3) -> pd.DataFrame | None:
    """Return the filtered rows of one day, downloading them if they are not cached yet."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, day)
    if path.exists():
        return _read_cache(path)

    url = BASE_URL.format(year=day.year, day=day.isoformat())
    http = session or requests.Session()
    for attempt in range(1, retries + 1):
        try:
            resp = http.get(url, timeout=120)
            if resp.status_code == 404:
                log.warning("No LCSQA file for %s", day)
                return None
            resp.raise_for_status()
            df = parse(resp.content, flt)
            df.to_csv(path, index=False, compression="gzip")
            return df
        except requests.RequestException as exc:
            log.warning("Download of %s failed (attempt %d/%d): %s", day, attempt, retries, exc)
            time.sleep(2 * attempt)
    return None


def fetch_range(start: date, end: date, cache_dir: Path, flt: LcsqaFilter = LcsqaFilter()) -> pd.DataFrame:
    session = requests.Session()
    session.headers["User-Agent"] = "aeria-data-pipeline/1.0 (open-data research project)"
    frames = []
    day = start
    while day <= end:
        df = fetch_day(day, cache_dir, flt, session)
        if df is not None and not df.empty:
            frames.append(df)
        day += timedelta(days=1)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["measured_at"] = pd.to_datetime(out["measured_at"], utc=True)
    return out


def main() -> None:  # python -m aeria_pipeline.extract.lcsqa 2025-11-01 2026-11-11 D:/cache
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    start, end, cache = date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2]), Path(sys.argv[3])
    df = fetch_range(start, end, cache)
    log.info("%d rows, %d stations, %s -> %s", len(df), df["station_code"].nunique() if len(df) else 0, start, end)


if __name__ == "__main__":
    main()
