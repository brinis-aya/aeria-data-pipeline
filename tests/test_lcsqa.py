from datetime import date

import pandas as pd

from aeria_pipeline.extract import lcsqa

HEADER = ("Date de début;Date de fin;Organisme;code zas;Zas;code site;nom site;type d'implantation;"
          "Polluant;type d'influence;discriminant;Réglementaire;type d'évaluation;procédure de mesure;"
          "type de valeur;valeur;valeur brute;unité de mesure;taux de saisie;couverture temporelle;"
          "couverture de données;code qualité;validité")


def row(agency="ATMO NORMANDIE", pollutant="NO2", value="12,5", validity="1", start="2026/11/11 05:00:00",
        value_type="moyenne horaire brute", site="FR05089"):
    return (f"{start};2026/11/11 06:00:00;{agency};FR28ZAR01;ZAR LE-HAVRE;{site};Le Havre Lafaurie-montant;"
            f"Urbaine;{pollutant};Trafic;A;Oui;mesures fixes;x;{value_type};{value};{value};µg-m3;;;;A;{validity}")


def csv(*rows):
    return ("﻿" + "\n".join([HEADER, *rows])).encode("utf-8")


def test_parse_filters_agency_pollutant_and_value_type():
    df = lcsqa.parse(csv(row(), row(agency="AIRPARIF"), row(pollutant="CO"),
                         row(value_type="moyenne journalière")))
    assert len(df) == 1
    r = df.iloc[0]
    assert r.station_code == "FR05089" and r.pollutant == "no2"
    assert r.value == 12.5  # French decimal comma
    assert r.unit == "µg/m³"


def test_timestamps_are_utc():
    df = lcsqa.parse(csv(row(start="2026/07/01 05:00:00")))
    assert str(df.measured_at.dt.tz) == "UTC"
    assert df.measured_at.iloc[0] == pd.Timestamp("2026-07-01 05:00", tz="UTC")


def test_validity_codes_1_and_4_are_valid_minus_1_is_not():
    df = lcsqa.parse(csv(row(validity="1"), row(validity="4", start="2026/11/11 06:00:00"),
                         row(validity="-1", value="", start="2026/11/11 07:00:00")))
    assert df.valid.tolist() == [True, True, False]
    assert df.validity.tolist() == [1, 4, -1]


def test_cache_round_trip(tmp_path):
    class Resp:
        status_code = 200
        content = csv(row(), row(validity="-1", value="", start="2026/11/11 06:00:00"))

        def raise_for_status(self):
            pass

    class Session:
        calls = 0

        def get(self, url, timeout):
            Session.calls += 1
            assert url.endswith("/2026/FR_E2_2026-11-11.csv")
            return Resp()

    first = lcsqa.fetch_day(date(2026, 11, 11), tmp_path, session=Session())
    second = lcsqa.fetch_day(date(2026, 11, 11), tmp_path, session=Session())
    assert Session.calls == 1  # second read comes from the gzip cache
    assert second.valid.tolist() == first.valid.tolist() == [True, False]
    assert second.station_code.iloc[0] == "FR05089"
