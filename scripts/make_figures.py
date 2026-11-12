"""Generate the report figures (docs/figures/*.png) from the warehouse.

    python scripts/make_figures.py [--dw postgresql://aeria:aeria@127.0.0.1:5440/aeria]
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
import psycopg  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "docs" / "figures"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]  # fixed categorical order
# Official ATMO index colours (arrêté du 10/07/2020) - the domain convention readers expect.
ATMO = {"Bon": "#50F0E6", "Moyen": "#50CCAA", "Dégradé": "#F0E641", "Mauvais": "#FF5050",
        "Très mauvais": "#960032", "Extrêmement mauvais": "#872181"}

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "text.color": INK, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.spines.top": False, "axes.spines.right": False, "font.size": 10,
    "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "lines.linewidth": 2, "legend.frameon": False,
})


def q(conn, sql: str) -> pd.DataFrame:
    cur = conn.execute(sql)
    return pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])


def save(fig, name: str) -> None:
    fig.tight_layout()
    fig.savefig(OUT / name, dpi=150)
    plt.close(fig)
    print("wrote", OUT / name)


def fig_monthly(conn) -> None:
    df = q(conn, """
        SELECT date_trunc('month', ts_utc AT TIME ZONE 'Europe/Paris')::date AS month, station_name,
               pollutant, avg(value) AS mean
        FROM dw.v_measurement
        WHERE station_code IN ('FR05089','FR05090') AND pollutant IN ('no2','pm25') AND usable
        GROUP BY 1, 2, 3 ORDER BY 1""")
    df["month"] = pd.to_datetime(df["month"])
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.8), sharey=True)
    for ax, (pol, label) in zip(axes, [("no2", "NO₂"), ("pm25", "PM2.5")]):
        for i, (st, g) in enumerate(df[df.pollutant == pol].groupby("station_name")):
            ax.plot(g["month"], g["mean"].astype(float), color=SERIES[i], marker="o", markersize=5, label=st)
        ax.set_title(f"{label} - moyenne mensuelle (µg/m³)")
        ax.tick_params(axis="x", rotation=45)
    axes[0].legend(loc="upper right")
    save(fig, "01_le_havre_mensuel.png")


def fig_profile(conn) -> None:
    df = q(conn, """
        SELECT hour_local, is_weekend, avg(value) AS no2
        FROM dw.v_measurement WHERE pollutant = 'no2' AND usable AND is_reference
        GROUP BY 1, 2 ORDER BY 1""")
    fig, ax = plt.subplots(figsize=(8, 3.8))
    for i, (we, label) in enumerate([(False, "Jours ouvrés"), (True, "Week-end")]):
        g = df[df.is_weekend == we]
        ax.plot(g["hour_local"], g["no2"].astype(float), color=SERIES[i], label=label)
    ax.set_xticks(range(0, 24, 2))
    ax.set_xlim(0, 23)
    ax.set_xlabel("Heure locale")
    ax.set_title("NO₂ - profil journalier moyen, stations de référence de Normandie (µg/m³)")
    ax.legend(loc="upper left")
    save(fig, "02_profil_no2.png")


def fig_atmo(conn) -> None:
    df = q(conn, """
        SELECT s.station_name, f.atmo_label, count(*) AS days
        FROM dw.fact_daily_index f JOIN dw.dim_station s USING (station_key)
        WHERE s.station_code IN ('FR05089','FR05090','FR05037','LH-001','LH-002','LH-003')
              AND f.n_pollutants >= 2
        GROUP BY 1, 2""")
    pivot = df.pivot(index="station_name", columns="atmo_label", values="days").fillna(0)
    pivot = pivot[[c for c in ATMO if c in pivot]]
    share = pivot.div(pivot.sum(axis=1), axis=0) * 100
    fig, ax = plt.subplots(figsize=(10, 3.8))
    left = pd.Series(0.0, index=share.index)
    for label in share.columns:
        ax.barh(share.index, share[label], left=left, color=ATMO[label], label=label,
                edgecolor=SURFACE, linewidth=2, height=0.6)
        for st, v in share[label].items():
            if v >= 8:
                ax.text(left[st] + v / 2, st, f"{v:.0f} %", ha="center", va="center", fontsize=8,
                        color="#ffffff" if label in ("Très mauvais", "Extrêmement mauvais") else INK)
        left += share[label]
    ax.set_xlim(0, 100)
    ax.set_xlabel("% des jours")
    ax.grid(axis="y", visible=False)
    ax.set_title("Indice ATMO journalier - Le Havre (stations de référence et capteurs Aeria)")
    ax.legend(ncol=len(share.columns), loc="upper center", bbox_to_anchor=(0.5, -0.22), fontsize=9)
    save(fig, "03_indice_atmo.png")


def fig_quality(conn) -> None:
    df = q(conn, """
        SELECT src.code AS source, p.code AS pollutant, q.code AS quality, count(*) AS n
        FROM dw.fact_measurement f
        JOIN dw.dim_source src USING (source_key) JOIN dw.dim_pollutant p USING (pollutant_key)
        JOIN dw.dim_quality q USING (quality_key)
        WHERE NOT q.usable OR q.code = 'clipped'
        GROUP BY 1, 2, 3""")
    tot = q(conn, """SELECT src.code AS source, p.code AS pollutant, count(*) AS total
                     FROM dw.fact_measurement f JOIN dw.dim_source src USING (source_key)
                     JOIN dw.dim_pollutant p USING (pollutant_key) GROUP BY 1, 2""")
    df = df.merge(tot, on=["source", "pollutant"])
    df["pct"] = 100 * df["n"] / df["total"]
    df["serie"] = df["source"] + " · " + df["pollutant"]
    pivot = df.pivot(index="serie", columns="quality", values="pct").fillna(0)
    order = [c for c in ["invalid", "clipped", "spike", "frozen", "range"] if c in pivot]
    pivot = pivot[order]
    fig, ax = plt.subplots(figsize=(10, 4))
    left = pd.Series(0.0, index=pivot.index)
    for i, col in enumerate(order):
        ax.barh(pivot.index, pivot[col], left=left, color=SERIES[i], label=col,
                edgecolor=SURFACE, linewidth=2, height=0.6)
        left += pivot[col]
    for st, v in left.items():
        ax.text(v + 0.1, st, f"{v:.1f} %", va="center", fontsize=8, color=INK2)
    ax.set_xlabel("% des heures")
    ax.grid(axis="y", visible=False)
    ax.set_title("Valeurs signalées par le contrôle qualité, par source et polluant")
    ax.legend(ncol=len(order), loc="upper center", bbox_to_anchor=(0.5, -0.18))
    save(fig, "04_qualite.png")


def fig_sensors(conn) -> None:
    df = q(conn, """
        SELECT date_trunc('day', ts_utc)::date AS day, is_reference, avg(value) AS pm25
        FROM dw.v_measurement
        WHERE pollutant = 'pm25' AND usable
          AND (station_code LIKE 'LH-%' OR station_code IN ('FR05089','FR05090'))
          AND ts_utc >= '2026-07-15'
        GROUP BY 1, 2 ORDER BY 1""")
    df["day"] = pd.to_datetime(df["day"])
    fig, ax = plt.subplots(figsize=(10, 3.8))
    for i, (ref, label) in enumerate([(True, "Stations de référence (ATMO Normandie)"),
                                       (False, "Capteurs Aeria (simulés)")]):
        g = df[df.is_reference == ref]
        ax.plot(g["day"], g["pm25"].astype(float), color=SERIES[i], label=label)
    ax.set_title("PM2.5 journalier au Havre - capteurs simulés vs mesures réelles (µg/m³)")
    ax.legend(loc="upper left")
    save(fig, "05_capteurs_vs_reference.png")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dw", default=os.getenv("DW_URL", "postgresql://aeria:aeria@127.0.0.1:5440/aeria"))
    args = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(args.dw) as conn:
        for f in (fig_monthly, fig_profile, fig_atmo, fig_quality, fig_sensors):
            f(conn)


if __name__ == "__main__":
    main()
