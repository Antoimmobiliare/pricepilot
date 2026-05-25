"""
PricePilot - Analytics Analyzer
Analisi storica prezzi, trend di mercato e metriche KPI.
Usa pandas per aggregazioni e calcoli efficienti.
"""
import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

from pricepilot.core.database import (
    get_decisions,
    get_market_snapshots,
    get_competitors,
    get_summary_stats,
)

logger = logging.getLogger("pricepilot.analytics")


# ─── Caricamento dati ─────────────────────────────────────────────────────────

def load_decisions_df(
    limit: int = 500,
    date_from: str = None,
    date_to: str = None,
    account_id: int = None,
) -> pd.DataFrame:
    """Carica decisioni di pricing in un DataFrame pandas."""
    rows = get_decisions(
        limit=limit,
        date_from=date_from,
        date_to=date_to,
        account_id=account_id,
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"]      = pd.to_datetime(df["date"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["pct_change_display"] = (df["pct_change"] * 100).round(1)
    return df


def load_snapshots_df(limit: int = 90) -> pd.DataFrame:
    rows = get_market_snapshots(limit=limit)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"]      = pd.to_datetime(df["date"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


# ─── KPI ─────────────────────────────────────────────────────────────────────

def compute_kpis(df: pd.DataFrame) -> Dict:
    """Calcola KPI principali da un DataFrame di decisioni."""
    if df.empty:
        return {k: 0 for k in [
            "avg_price", "max_price", "min_price",
            "avg_pct_change", "positive_changes", "negative_changes",
            "total_decisions", "event_count", "avg_occupancy",
        ]}

    event_count = df["event"].notna() & ~df["event"].isin(["none", "0", "", "None"])
    return {
        "avg_price":        round(df["new_price"].mean(), 2),
        "max_price":        round(df["new_price"].max(), 2),
        "min_price":        round(df["new_price"].min(), 2),
        "avg_pct_change":   round((df["pct_change"] * 100).abs().mean(), 2),
        "positive_changes": int((df["pct_change"] > 0).sum()),
        "negative_changes": int((df["pct_change"] < 0).sum()),
        "total_decisions":  len(df),
        "event_count":      int(event_count.sum()),
        "avg_occupancy":    round(df["occupancy"].dropna().mean() * 100, 1),
    }


# ─── Serie temporali ─────────────────────────────────────────────────────────

def price_history_series(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ritorna una serie giornaliera con:
    our_price, market_price, competitor_min, competitor_max
    """
    if df.empty:
        return pd.DataFrame()
    daily = (
        df.sort_values("date")
          .groupby("date")
          .agg(
              our_price        = ("new_price", "last"),
              market_price     = ("market_price", "mean"),
              competitor_min   = ("competitor_min", "mean"),
              competitor_max   = ("competitor_max", "mean"),
              occupancy        = ("occupancy", "mean"),
          )
          .reset_index()
    )
    return daily


def price_vs_market_df(df: pd.DataFrame) -> pd.DataFrame:
    """Confronto nostro prezzo vs mercato con differenza percentuale."""
    daily = price_history_series(df)
    if daily.empty:
        return pd.DataFrame()
    daily["vs_market_pct"] = (
        (daily["our_price"] - daily["market_price"])
        / daily["market_price"].replace(0, float("nan"))
        * 100
    ).round(2)
    return daily


def occupancy_price_correlation(df: pd.DataFrame) -> pd.DataFrame:
    """Correlazione occupancy → prezzo."""
    if df.empty:
        return pd.DataFrame()
    return (
        df[["occupancy", "new_price", "event"]]
          .dropna(subset=["occupancy"])
          .copy()
    )


def strategy_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    """Distribuzione decisioni per strategia."""
    if df.empty:
        return pd.DataFrame()
    return (
        df.groupby("strategy")
          .agg(
              count     = ("id", "count"),
              avg_price = ("new_price", "mean"),
              avg_change = ("pct_change", lambda x: (x * 100).mean()),
          )
          .reset_index()
          .round(2)
    )


def event_impact_analysis(df: pd.DataFrame) -> pd.DataFrame:
    """Analisi impatto eventi sul prezzo medio."""
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["has_event"] = df["event"].notna() & ~df["event"].isin(["none", "0", "", "None"])
    return (
        df.groupby("has_event")
          .agg(
              avg_price     = ("new_price", "mean"),
              avg_occupancy = ("occupancy", "mean"),
              count         = ("id", "count"),
          )
          .reset_index()
          .round(2)
    )


def rolling_avg_price(df: pd.DataFrame, window: int = 7) -> pd.DataFrame:
    """Media mobile prezzi."""
    daily = price_history_series(df)
    if daily.empty:
        return pd.DataFrame()
    daily = daily.sort_values("date")
    daily[f"ma_{window}d"] = daily["our_price"].rolling(window=window, min_periods=1).mean().round(2)
    return daily


# ─── Revenue Estimation ───────────────────────────────────────────────────────

def estimate_revenue(df: pd.DataFrame) -> Dict:
    """Stima revenue basata su prezzo × occupancy."""
    if df.empty:
        return {"total_est": 0, "avg_daily": 0, "best_month": "N/A"}
    df = df.copy()
    df["est_revenue"] = df["new_price"] * df["occupancy"].fillna(0.7)
    total  = round(df["est_revenue"].sum(), 2)
    avg    = round(df["est_revenue"].mean(), 2)
    df["month"] = df["date"].dt.to_period("M").astype(str)
    best_month = df.groupby("month")["est_revenue"].sum().idxmax() if len(df) else "N/A"
    return {
        "total_est":  total,
        "avg_daily":  avg,
        "best_month": best_month,
    }
