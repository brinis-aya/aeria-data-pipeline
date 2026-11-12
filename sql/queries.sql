-- Example analyses on the Aeria warehouse (results are quoted in docs/rapport.md)

-- Q1. Monthly mean of usable PM2.5 and NO2 at the two Le Havre reference stations
SELECT date_trunc('month', ts_utc AT TIME ZONE 'Europe/Paris')::date AS month,
       station_name, pollutant, round(avg(value)::numeric, 1) AS mean_ugm3, count(*) AS hours
FROM dw.v_measurement
WHERE station_code IN ('FR05089', 'FR05090') AND pollutant IN ('pm25', 'no2') AND usable
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

-- Q2. Daily profile of NO2: working days vs weekend (traffic signature)
SELECT hour_local, is_weekend, round(avg(value)::numeric, 1) AS no2_mean
FROM dw.v_measurement
WHERE pollutant = 'no2' AND usable AND is_reference
GROUP BY 1, 2
ORDER BY 2, 1;

-- Q3. Distribution of the daily ATMO index (full indices only) per station
SELECT s.station_name, f.atmo_label, count(*) AS days,
       round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY s.station_name), 1) AS pct
FROM dw.fact_daily_index f
JOIN dw.dim_station s ON s.station_key = f.station_key
WHERE f.n_pollutants >= 3
GROUP BY 1, 2, f.atmo_index
ORDER BY 1, f.atmo_index;

-- Q4. Days above the WHO 2021 daily PM2.5 guideline (15 µg/m³), by season (reference stations)
SELECT d.season, count(*) FILTER (WHERE f.pm25_conc > p.who_daily_guideline) AS days_above,
       count(f.pm25_conc) AS days_measured
FROM dw.fact_daily_index f
JOIN dw.dim_date d ON d.date_key = f.date_key
JOIN dw.dim_station s ON s.station_key = f.station_key
CROSS JOIN (SELECT who_daily_guideline FROM dw.dim_pollutant WHERE code = 'pm25') p
WHERE s.is_reference
GROUP BY d.season
ORDER BY days_above DESC;

-- Q5. Data quality by source (share of each flag)
SELECT src.code AS source, q.code AS quality, count(*) AS rows,
       round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY src.code), 2) AS pct
FROM dw.fact_measurement f
JOIN dw.dim_source src ON src.source_key = f.source_key
JOIN dw.dim_quality q  ON q.quality_key = f.quality_key
GROUP BY 1, 2, q.quality_key
ORDER BY 1, q.quality_key;

-- Q6. Low-cost sensors vs nearest reference station (monthly PM2.5 bias)
WITH m AS (
    SELECT date_trunc('month', ts_utc)::date AS month, is_reference, avg(value) AS pm25
    FROM dw.v_measurement
    WHERE pollutant = 'pm25' AND usable
      AND (station_code LIKE 'LH-%' OR station_code IN ('FR05089', 'FR05090'))
    GROUP BY 1, 2
)
SELECT month,
       round(max(pm25) FILTER (WHERE NOT is_reference)::numeric, 1) AS sensors,
       round(max(pm25) FILTER (WHERE is_reference)::numeric, 1)     AS reference
FROM m GROUP BY month HAVING count(*) = 2 ORDER BY month;

-- Q7. Pipeline runs (lineage)
SELECT run_id, started_at, finished_at - started_at AS duration, period_start, period_end,
       sources, rows_extracted, rows_loaded, rows_usable, status
FROM dw.etl_run ORDER BY run_id DESC LIMIT 10;
