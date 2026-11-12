# Aeria · Data pipeline

[![CI](https://github.com/brinis-aya/aeria-data-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/brinis-aya/aeria-data-pipeline/actions/workflows/ci.yml)

Module 2 of **Aeria**, an open air-quality monitoring platform. This repository is the ETL: it pulls
**official French open data** (LCSQA / Geod'air hourly measurements published on data.gouv.fr) and the
readings of the Aeria IoT sensors, cleans them with **pandas**, computes the French **ATMO index**, and
loads everything into a **PostgreSQL star-schema warehouse**.

> Other modules: [IoT sensors](https://github.com/brinis-aya/aeria-iot-sensors) ·
> [24 h forecast](https://github.com/brinis-aya/aeria-forecast) ·
> [back-office](https://github.com/brinis-aya/aeria-backoffice) ·
> [security](https://github.com/brinis-aya/aeria-security) ·
> [dashboard](https://github.com/brinis-aya/aeria-dashboard) ·
> [overview](https://github.com/brinis-aya/aeria)

```
 data.gouv.fr  FR_E2_YYYY-MM-DD.csv ─┐  extract            transform                         load
 (LCSQA, all France, ~12 MB/day)     ├─▶ filter + cache ─▶ hourly · dedup · quality flags ─▶ dw.* star schema
 aeria-iot-sensors (PostgreSQL/API) ─┘   (gzip, 47 MB/yr)   ATMO daily index                  (COPY + upsert)
```

## Results on real data (1 Nov 2025 → 11 Nov 2026, Normandy)

| | |
|---|---|
| Hourly values loaded | **902,607** (36 reference stations + 6 Aeria sensors) |
| Usable after cleaning | **95.7 %** (863,766) |
| Daily ATMO indices | **13,543** station-days (8,222 with ≥ 3 pollutants) |
| Full-year run | 2 min 26 s (extract 3 s from cache, transform 3 s, load 2 min 17 s) |
| Re-run of a period | idempotent: same row counts, values updated in place |
| Tests | 35 (pytest), including a PostgreSQL integration test in CI |

<p align="center"><img src="docs/figures/02_profil_no2.png" width="640" alt="NO2 daily profile, working days vs weekend"></p>

The NO₂ profile shows the traffic signature (8 h and 18 h peaks on working days, none on weekends); on
reference stations with a full index, the daily ATMO index was *Bon* or *Moyen* 85 % of the time, and
ozone was the pollutant responsible on 55 % of the days. Full analysis (in French): [`docs/rapport.md`](docs/rapport.md).

## Data sources

| Source | What | Licence |
|---|---|---|
| [LCSQA / Geod'air – real-time data](https://www.data.gouv.fr/fr/datasets/donnees-temps-reel-de-mesure-des-concentrations-de-polluants-atmospheriques-reglementes-1/) | hourly NO₂, O₃, PM10, PM2.5, SO₂ of every regulatory station in France | Licence Ouverte 2.0 |
| [aeria-iot-sensors](https://github.com/brinis-aya/aeria-iot-sensors) | 15-min readings of the Aeria sensors (simulated around Le Havre) | MIT |

Only the selected agencies (default: `ATMO NORMANDIE`) are kept from each daily national file, and the
filtered rows are cached gzip-compressed, so a full year takes 47 MB instead of 4.5 GB.

## Cleaning

Every row is kept and gets exactly one quality flag, so the warehouse can always explain why a value
was excluded:

| Flag | Rule |
|---|---|
| `invalid` | producer validity code `-1` or no value (codes `1` valid and `4` pre-validated are kept) |
| `range` | above a physical limit (e.g. PM2.5 > 1000 µg/m³) or below −2 µg/m³ |
| `spike` | Hampel filter: > 4 robust σ from the 7-hour rolling median **and** > 25 µg/m³ |
| `frozen` | same non-zero value for ≥ 6 consecutive hours (stuck sensor) |
| `clipped` | small negative (instrument noise around zero), set to 0 |
| `ok` | usable as is |

Sensor readings are first averaged per hour with the regulatory 75 % coverage rule (≥ 3 of 4 readings).

## ATMO index

Implemented from the French decree of 10 July 2020 (in force since 2021): six levels from *Bon* to
*Extrêmement mauvais*; PM daily means, NO₂/O₃/SO₂ daily maxima of the hourly means, local days
(Europe/Paris), at least 18 valid hours per day. The global index is the worst sub-index and the
responsible pollutant is stored. Thresholds are in [`aeria_pipeline/transform/atmo.py`](aeria_pipeline/transform/atmo.py).

## Warehouse

Star schema in [`sql/schema.sql`](sql/schema.sql), grain = one hour × station × pollutant × source:

```
            dim_date ◀── dim_time
                            ▲
 dim_station ◀── fact_measurement ──▶ dim_pollutant        fact_daily_index ──▶ dim_date, dim_station,
                    │         │                                                  dim_pollutant (responsible)
              dim_source   dim_quality                       etl_run (lineage: one row per run)
```

`dw.v_measurement` joins everything for ad-hoc analysis; [`sql/queries.sql`](sql/queries.sql) holds the
analyses quoted in the report (monthly means, daily profiles, ATMO distribution, WHO guideline
exceedances, sensor vs reference bias, run history).

## Run it

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
docker compose up -d                              # PostgreSQL on 127.0.0.1:5441
export DW_URL=postgresql://aeria:aeria@127.0.0.1:5441/aeria

aeria-pipeline run --sources lcsqa --start 2026-10-01 --end 2026-10-31
python scripts/make_figures.py                    # regenerates docs/figures/
```

With module 1 running (`docker compose up` in `aeria-iot-sensors`), add `aeria-iot` to `--sources`
(default: both) and point `DW_URL`/`--iot-db` at its database on port 5440. Other options:
`--agency "ATMO NORMANDIE" "AIRPARIF"`, `--cache DIR`, `--no-load` (CSV reports in `out/` only).

```bash
pytest                                            # unit tests
DW_TEST_URL=postgresql://aeria:aeria@127.0.0.1:5441/aeria pytest   # + warehouse integration test
```

CI runs the tests against a PostgreSQL service container and, every morning, an end-to-end run on the
last days of real open data.

## Layout

```
aeria_pipeline/
  extract/lcsqa.py      download, filter and cache the daily national files
  extract/sensors.py    read the Aeria sensors (database or REST export)
  transform/clean.py    hourly aggregation, quality flags, completeness
  transform/atmo.py     ATMO sub-indices and daily index
  load/warehouse.py     dimensions, COPY-based fact upserts, run lineage
  cli.py                aeria-pipeline run | init-db
sql/                    schema.sql, queries.sql
scripts/make_figures.py report figures
docs/rapport.md         report (French)
```

## License

MIT. Data: LCSQA / Geod'air under Licence Ouverte 2.0 — "Source : LCSQA / Geod'air".
