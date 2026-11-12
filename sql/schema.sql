-- Aeria data warehouse - star schema (PostgreSQL 15+)
-- Grain of the main fact: one cleaned hourly value per (hour, station, pollutant, source).

CREATE SCHEMA IF NOT EXISTS dw;

-- ---------------------------------------------------------------- dimensions

CREATE TABLE IF NOT EXISTS dw.dim_date (
    date_key     INTEGER PRIMARY KEY,           -- yyyymmdd (local date, Europe/Paris)
    date_local   DATE NOT NULL UNIQUE,
    year         SMALLINT NOT NULL,
    quarter      SMALLINT NOT NULL,
    month        SMALLINT NOT NULL,
    month_name   VARCHAR(12) NOT NULL,
    iso_week     SMALLINT NOT NULL,
    day_of_week  SMALLINT NOT NULL,             -- 1 = Monday ... 7 = Sunday
    is_weekend   BOOLEAN NOT NULL,
    season       VARCHAR(10) NOT NULL           -- winter / spring / summer / autumn
);

CREATE TABLE IF NOT EXISTS dw.dim_time (
    time_key     BIGINT PRIMARY KEY,            -- yyyymmddhh (UTC)
    ts_utc       TIMESTAMPTZ NOT NULL UNIQUE,
    date_key     INTEGER NOT NULL REFERENCES dw.dim_date(date_key),
    hour_local   SMALLINT NOT NULL,             -- 0-23, Europe/Paris
    is_rush_hour BOOLEAN NOT NULL               -- 7-9 h and 17-19 h local, working days
);

CREATE TABLE IF NOT EXISTS dw.dim_source (
    source_key   SMALLSERIAL PRIMARY KEY,
    code         VARCHAR(32) NOT NULL UNIQUE,
    label        VARCHAR(128) NOT NULL,
    licence      VARCHAR(64),
    url          TEXT
);

CREATE TABLE IF NOT EXISTS dw.dim_station (
    station_key  SERIAL PRIMARY KEY,
    station_code VARCHAR(32) NOT NULL UNIQUE,
    station_name VARCHAR(128),
    agency       VARCHAR(64),
    zone         VARCHAR(64),
    area_type    VARCHAR(32),                   -- Urbaine, Périurbaine, Rurale ..., or sensor environment
    influence    VARCHAR(32),                   -- Fond, Trafic, Industrielle, Capteur basse coût
    latitude     DOUBLE PRECISION,
    longitude    DOUBLE PRECISION,
    is_reference BOOLEAN NOT NULL               -- regulatory analyser (true) or low-cost sensor
);

CREATE TABLE IF NOT EXISTS dw.dim_pollutant (
    pollutant_key SMALLSERIAL PRIMARY KEY,
    code          VARCHAR(8) NOT NULL UNIQUE,   -- pm25, pm10, no2, o3, so2
    label         VARCHAR(64) NOT NULL,
    unit          VARCHAR(16) NOT NULL,
    who_daily_guideline DOUBLE PRECISION        -- OMS 2021 (24 h, or 8 h for O3), µg/m³
);

CREATE TABLE IF NOT EXISTS dw.dim_quality (
    quality_key  SMALLINT PRIMARY KEY,
    code         VARCHAR(16) NOT NULL UNIQUE,
    usable       BOOLEAN NOT NULL,
    description  TEXT NOT NULL
);

-- ---------------------------------------------------------------- facts

CREATE TABLE IF NOT EXISTS dw.fact_measurement (
    time_key      BIGINT   NOT NULL REFERENCES dw.dim_time(time_key),
    station_key   INTEGER  NOT NULL REFERENCES dw.dim_station(station_key),
    pollutant_key SMALLINT NOT NULL REFERENCES dw.dim_pollutant(pollutant_key),
    source_key    SMALLINT NOT NULL REFERENCES dw.dim_source(source_key),
    quality_key   SMALLINT NOT NULL REFERENCES dw.dim_quality(quality_key),
    value_raw     DOUBLE PRECISION,             -- as published
    value         DOUBLE PRECISION,             -- cleaned (NULL when not usable)
    run_id        INTEGER  NOT NULL,
    PRIMARY KEY (time_key, station_key, pollutant_key, source_key)
);
CREATE INDEX IF NOT EXISTS ix_fact_measurement_station ON dw.fact_measurement (station_key, pollutant_key, time_key);

CREATE TABLE IF NOT EXISTS dw.fact_daily_index (
    date_key      INTEGER  NOT NULL REFERENCES dw.dim_date(date_key),
    station_key   INTEGER  NOT NULL REFERENCES dw.dim_station(station_key),
    atmo_index    SMALLINT NOT NULL CHECK (atmo_index BETWEEN 1 AND 6),
    atmo_label    VARCHAR(24) NOT NULL,
    responsible_pollutant_key SMALLINT REFERENCES dw.dim_pollutant(pollutant_key),
    n_pollutants  SMALLINT NOT NULL,              -- sub-indices available (5 = full index)
    pm25_conc DOUBLE PRECISION, pm25_index SMALLINT,
    pm10_conc DOUBLE PRECISION, pm10_index SMALLINT,
    no2_conc  DOUBLE PRECISION, no2_index  SMALLINT,
    o3_conc   DOUBLE PRECISION, o3_index   SMALLINT,
    so2_conc  DOUBLE PRECISION, so2_index  SMALLINT,
    run_id        INTEGER NOT NULL,
    PRIMARY KEY (date_key, station_key)
);

-- ---------------------------------------------------------------- lineage

CREATE TABLE IF NOT EXISTS dw.etl_run (
    run_id        SERIAL PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    period_start  DATE,
    period_end    DATE,
    sources       TEXT,
    rows_extracted INTEGER,
    rows_loaded    INTEGER,
    rows_usable    INTEGER,
    status        VARCHAR(16) NOT NULL DEFAULT 'running'
);

-- ---------------------------------------------------------------- reference data

INSERT INTO dw.dim_quality (quality_key, code, usable, description) VALUES
    (0, 'ok',      TRUE,  'Valeur utilisable telle quelle'),
    (1, 'clipped', TRUE,  'Petite valeur négative (bruit autour de zéro) ramenée à 0'),
    (2, 'invalid', FALSE, 'Invalidée par le producteur ou valeur absente'),
    (3, 'range',   FALSE, 'Hors plage physiquement plausible'),
    (4, 'spike',   FALSE, 'Pic isolé détecté par filtre de Hampel'),
    (5, 'frozen',  FALSE, 'Valeur identique répétée (capteur bloqué)')
ON CONFLICT (quality_key) DO NOTHING;

INSERT INTO dw.dim_pollutant (code, label, unit, who_daily_guideline) VALUES
    ('pm25', 'Particules fines PM2.5',  'µg/m³', 15),
    ('pm10', 'Particules PM10',         'µg/m³', 45),
    ('no2',  'Dioxyde d''azote NO2',    'µg/m³', 25),
    ('o3',   'Ozone O3',                'µg/m³', 100),
    ('so2',  'Dioxyde de soufre SO2',   'µg/m³', 40)
ON CONFLICT (code) DO NOTHING;

INSERT INTO dw.dim_source (code, label, licence, url) VALUES
    ('lcsqa',     'LCSQA / Geod''air - données temps réel (data.gouv.fr)', 'Licence Ouverte 2.0',
     'https://www.data.gouv.fr/fr/datasets/donnees-temps-reel-de-mesure-des-concentrations-de-polluants-atmospheriques-reglementes-1/'),
    ('aeria-iot', 'Capteurs Aeria (module 1, MQTT)', 'MIT', 'https://github.com/brinis-aya/aeria-iot-sensors')
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------- analysis views

CREATE OR REPLACE VIEW dw.v_measurement AS
SELECT t.ts_utc, d.date_local, t.hour_local, d.day_of_week, d.is_weekend, d.month, d.season,
       s.station_code, s.station_name, s.zone, s.influence, s.is_reference,
       p.code AS pollutant, src.code AS source, q.code AS quality, q.usable,
       f.value_raw, f.value
FROM dw.fact_measurement f
JOIN dw.dim_time t       ON t.time_key = f.time_key
JOIN dw.dim_date d       ON d.date_key = t.date_key
JOIN dw.dim_station s    ON s.station_key = f.station_key
JOIN dw.dim_pollutant p  ON p.pollutant_key = f.pollutant_key
JOIN dw.dim_source src   ON src.source_key = f.source_key
JOIN dw.dim_quality q    ON q.quality_key = f.quality_key;
