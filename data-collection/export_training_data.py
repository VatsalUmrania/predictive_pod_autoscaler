"""
Collects metrics from Prometheus and saves as CSV for ML training.
Runs inside the cluster - no port-forwarding needed.
"""
import argparse
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

from config import PROMETHEUS_URL, QUERIES, TARGET_APP

TARGET_COLUMNS = [
    "rps_t5",
    "rps_t10",
    "rps_t15",
    "replicas_t5",
    "replicas_t10",
    "replicas_t15",
]
FEATURE_COLUMNS = list(QUERIES.keys())


def step_to_seconds(step: str) -> int:
    step = step.strip().lower()
    if step.endswith("s"):
        return int(step[:-1])
    if step.endswith("m"):
        return int(step[:-1]) * 60
    raise ValueError(f"Unsupported step format: {step}. Use values like '15s' or '1m'.")


def step_to_pandas_freq(step: str) -> str:
    step = step.strip().lower()
    if step.endswith("s"):
        return f"{int(step[:-1])}s"
    if step.endswith("m"):
        return f"{int(step[:-1])}min"
    raise ValueError(f"Unsupported step format: {step}. Use values like '15s' or '1m'.")


def collect_range(query: str, hours: int = 24, step: str = "1m") -> pd.Series:
    end = datetime.now(timezone.utc)

    # Calculate data points to prevent silent Prometheus 11k limit failure
    step_seconds = step_to_seconds(step)
    requested_points = (hours * 3600) / step_seconds

    # Prometheus defaults to an 11,000 point limit per query
    if requested_points > 10000:
        safe_hours = (10000 * step_seconds) / 3600
        print(f"  WARNING: Requested {int(requested_points)} points. Prometheus limits queries to 11,000.")
        print(f"  WARNING: Capping query window to {safe_hours:.1f} hours to prevent silent failure.")
        start = end - timedelta(hours=safe_hours)
    else:
        start = end - timedelta(hours=hours)

    response = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query_range",
        params={
            "query": query,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": step,
        },
        timeout=30,
    )
    data = response.json().get("data", {}).get("result", [])
    if not data:
        return pd.Series(dtype=float)

    values = data[0]["values"]  # [[timestamp, value], ...]
    series = pd.Series(
        {datetime.fromtimestamp(ts, timezone.utc): float(val) for ts, val in values}
    )
    # Round so repeated runs dedupe cleanly.
    series.index = series.index.round(step_to_pandas_freq(step))
    return series


def resample_by_segment(
    df: pd.DataFrame, resample_freq: str, gap_threshold_minutes: int = 10
) -> pd.DataFrame:
    """
    Resample each continuous segment independently so large offline gaps do not
    produce synthetic rows across the gap.
    """
    if df.empty:
        return df

    df = df.sort_index()
    gap_threshold = pd.Timedelta(minutes=gap_threshold_minutes)
    segment_ids = (df.index.to_series().diff() > gap_threshold).cumsum()
    parts = [segment.resample(resample_freq).mean() for _, segment in df.groupby(segment_ids)]
    return pd.concat(parts).sort_index()


def add_temporal_features(df: pd.DataFrame) -> pd.DataFrame:
    df["hour_sin"] = np.sin(2 * np.pi * df.index.hour / 24)
    df["hour_cos"] = np.cos(2 * np.pi * df.index.hour / 24)
    df["dow_sin"] = np.sin(2 * np.pi * df.index.dayofweek / 7)
    df["dow_cos"] = np.cos(2 * np.pi * df.index.dayofweek / 7)
    df["is_weekend"] = (df.index.dayofweek >= 5).astype(int)
    return df


def _detect_segments(df: pd.DataFrame, gap_minutes: int = 10) -> pd.Series:
    """Return an integer Series labeling each row with its continuous segment ID."""
    gaps = df.index.to_series().diff() > pd.Timedelta(minutes=gap_minutes)
    return gaps.cumsum()


def add_prediction_targets(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build targets using exact timestamp lookups (t+5m, t+10m, t+15m),
    computed WITHIN each continuous segment so overnight gaps never
    produce NaN targets at segment boundaries.

    Rows at the tail of each segment (last 15 min) will still be dropped
    because there is genuinely no future data to predict. But rows from
    earlier segments are preserved instead of being poisoned by cross-gap
    reindex lookups.
    """
    df = df.sort_index()
    seg_ids = _detect_segments(df)
    parts = []

    for seg_id, seg in df.groupby(seg_ids):
        base = seg["requests_per_second"]
        seg = seg.copy()
        seg["rps_t5"] = base.reindex(seg.index + pd.Timedelta(minutes=5)).to_numpy()
        seg["rps_t10"] = base.reindex(seg.index + pd.Timedelta(minutes=10)).to_numpy()
        seg["rps_t15"] = base.reindex(seg.index + pd.Timedelta(minutes=15)).to_numpy()

        capacity_per_pod = 10
        seg["replicas_t5"] = np.ceil(seg["rps_t5"] / capacity_per_pod).clip(lower=2, upper=20)
        seg["replicas_t10"] = np.ceil(seg["rps_t10"] / capacity_per_pod).clip(lower=2, upper=20)
        seg["replicas_t15"] = np.ceil(seg["rps_t15"] / capacity_per_pod).clip(lower=2, upper=20)

        seg["segment_id"] = seg_id
        valid = seg.dropna(subset=["rps_t5", "rps_t10", "rps_t15"])
        if len(valid) > 0:
            parts.append(valid)

    if not parts:
        return df.iloc[0:0]  # empty with same columns
    return pd.concat(parts).sort_index()


def prepare_dataset(df: pd.DataFrame) -> pd.DataFrame:
    # Drop stale labels/aux columns before recomputing.
    stale_cols = [c for c in TARGET_COLUMNS + ["segment_id"] if c in df.columns]
    if stale_cols:
        df = df.drop(columns=stale_cols)

    df = df.sort_index()
    for feature_name in FEATURE_COLUMNS:
        if feature_name not in df.columns:
            df[feature_name] = 0.0

    # Fill metric holes at existing timestamps only (no new timestamps created).
    df[FEATURE_COLUMNS] = (
        df[FEATURE_COLUMNS].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    )

    df = add_temporal_features(df)
    df = add_prediction_targets(df)
    df.index.name = "timestamp"
    return df


def build_feature_dataframe(
    hours: int = 168, step: str = "1m", resample: str = None
) -> pd.DataFrame:
    print(f"Collecting {hours}h of data (step={step}) for app: {TARGET_APP}")
    feature_series = {}

    for feature_name, query in QUERIES.items():
        print(f"  Fetching {feature_name}...")
        series = collect_range(query, hours=hours, step=step)
        if not series.empty:
            feature_series[feature_name] = series
        else:
            print(f"  WARNING: No data for {feature_name} - skipping")

    if not feature_series:
        print("CRITICAL: No data could be retrieved from Prometheus for any metric.")
        return pd.DataFrame()

    # Build union index across all collected series.
    df = pd.DataFrame(feature_series).sort_index()

    if resample:
        print(f"Resampling data to {resample} intervals (segment-aware)...")
        df = resample_by_segment(df, step_to_pandas_freq(resample), gap_threshold_minutes=10)

    return prepare_dataset(df)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export PPA training data from Prometheus.")
    parser.add_argument("--hours", type=int, default=168, help="Hours of data to collect (default: 168)")
    parser.add_argument("--step", type=str, default="1m", help="Prometheus scrape step (default: 1m, try 15s)")
    parser.add_argument("--resample", type=str, default=None, help="Resample resulting dataframe (e.g., 1m)")
    args = parser.parse_args()

    df = build_feature_dataframe(hours=args.hours, step=args.step, resample=args.resample)
    if not df.empty:
        output_path = os.getenv("OUTPUT_PATH", "data-collection/training-data/training_data.csv")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        effective_step = args.resample if args.resample else args.step
        round_freq = step_to_pandas_freq(effective_step)

        # Safe append logic
        if os.path.exists(output_path):
            print(f"  Found existing dataset at {output_path}, safely appending new data...")
            df_existing = pd.read_csv(output_path, index_col="timestamp", parse_dates=True)
            df_combined = pd.concat([df_existing, df])
            df_combined.index = pd.to_datetime(df_combined.index).round(round_freq)
            df = df_combined[~df_combined.index.duplicated(keep="last")].sort_index()

        # NOTE: Do NOT call prepare_dataset() again here.
        # The new data already has valid targets from build_feature_dataframe().
        # The existing CSV rows already have valid targets from their original export.
        # Calling prepare_dataset() again would re-drop the last 15 min of the merged
        # dataset, causing the CSV to shrink on every run.
        df.to_csv(output_path)

        print(f"\n{'=' * 50}")
        print("Dataset Health Report")
        print(f"{'=' * 50}")
        print(f"Total rows       : {len(df)}")
        print(f"Total features   : {len(df.columns)}")
        print(f"Weekend rows     : {df['is_weekend'].sum()}")
        print(f"Weekday rows     : {(df['is_weekend'] == 0).sum()}")
        if len(df.index) > 1:
            max_gap = df.index.to_series().diff().dropna().max()
            print(f"Max time gap     : {max_gap}")
        print(f"Date range       : {df.index.min()} -> {df.index.max()}")
        print(f"{'=' * 50}")
    else:
        print("\nExport failed: DataFrame is empty. Ensure Prometheus contains data for the queries.")
