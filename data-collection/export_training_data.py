"""
Collect metrics from Prometheus and save a quality-gated training dataset.
Runs inside the cluster or locally against a port-forwarded Prometheus.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.constants import CAPACITY_PER_POD, GAP_THRESHOLD_MINUTES
from common.feature_spec import FEATURE_COLUMNS, QUERIED_FEATURES, TARGET_COLUMNS
from config import PROMETHEUS_URL, QUERIES, REQUIRED_QUERY_FEATURES, TARGET_APP


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

    step_seconds = step_to_seconds(step)
    requested_points = (hours * 3600) / step_seconds

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
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload}")

    data = payload.get("data", {}).get("result", [])
    if not data:
        return pd.Series(dtype=float)

    values = data[0]["values"]
    series = pd.Series(
        {datetime.fromtimestamp(ts, timezone.utc): float(val) for ts, val in values},
        dtype=float,
    )
    series.index = series.index.round(step_to_pandas_freq(step))
    return series


def resample_by_segment(
    df: pd.DataFrame, resample_freq: str, gap_threshold_minutes: int = GAP_THRESHOLD_MINUTES
) -> pd.DataFrame:
    """Resample each continuous segment independently."""
    if df.empty:
        return df

    df = df.sort_index()
    gap_threshold = pd.Timedelta(minutes=gap_threshold_minutes)
    segment_ids = (df.index.to_series().diff() > gap_threshold).cumsum()
    parts = [segment.resample(resample_freq).mean() for _, segment in df.groupby(segment_ids)]
    return pd.concat(parts).sort_index()


def add_temporal_features(df: pd.DataFrame) -> pd.DataFrame:
    utc_index = df.index.tz_convert(timezone.utc)
    df["hour_sin"] = np.sin(2 * np.pi * utc_index.hour / 24)
    df["hour_cos"] = np.cos(2 * np.pi * utc_index.hour / 24)
    df["dow_sin"] = np.sin(2 * np.pi * utc_index.dayofweek / 7)
    df["dow_cos"] = np.cos(2 * np.pi * utc_index.dayofweek / 7)
    df["is_weekend"] = (utc_index.dayofweek >= 5).astype(int)
    return df


def _detect_segments(df: pd.DataFrame, gap_minutes: int = GAP_THRESHOLD_MINUTES) -> pd.Series:
    gaps = df.index.to_series().diff() > pd.Timedelta(minutes=gap_minutes)
    return gaps.cumsum()


def drop_rows_missing_required_features(
    df: pd.DataFrame,
    required_features: list[str],
) -> tuple[pd.DataFrame, dict[str, int], int]:
    """Reject rows where required queried features are missing instead of filling with zero."""
    if df.empty:
        return df, {}, 0

    for feature_name in required_features:
        if feature_name not in df.columns:
            df[feature_name] = np.nan

    numeric_required = df[required_features].apply(pd.to_numeric, errors="coerce")
    missing_by_feature = numeric_required.isna().sum()
    drop_mask = numeric_required.isna().any(axis=1)
    dropped_rows = int(drop_mask.sum())

    if dropped_rows > 0:
        print(
            f"  WARNING: Dropping {dropped_rows} rows with missing required features "
            f"instead of coercing them to 0.0"
        )
        for feature_name, missing_count in missing_by_feature.items():
            if int(missing_count) > 0:
                print(f"    - {feature_name}: {int(missing_count)} missing values")

    df = df.loc[~drop_mask].copy()
    if df.empty:
        return df, {k: int(v) for k, v in missing_by_feature.items() if int(v) > 0}, dropped_rows

    df[required_features] = numeric_required.loc[df.index]
    return df, {k: int(v) for k, v in missing_by_feature.items() if int(v) > 0}, dropped_rows


def add_prediction_targets(df: pd.DataFrame) -> pd.DataFrame:
    """Build future targets within continuous segments only."""
    df = df.sort_index()
    seg_ids = _detect_segments(df)
    parts = []

    for seg_id, seg in df.groupby(seg_ids):
        base = seg["requests_per_second"]
        seg = seg.copy()
        seg["rps_t5"] = base.reindex(seg.index + pd.Timedelta(minutes=5)).to_numpy()
        seg["rps_t10"] = base.reindex(seg.index + pd.Timedelta(minutes=10)).to_numpy()
        seg["rps_t15"] = base.reindex(seg.index + pd.Timedelta(minutes=15)).to_numpy()

        seg["replicas_t5"] = np.ceil(seg["rps_t5"] / CAPACITY_PER_POD).clip(lower=2, upper=20)
        seg["replicas_t10"] = np.ceil(seg["rps_t10"] / CAPACITY_PER_POD).clip(lower=2, upper=20)
        seg["replicas_t15"] = np.ceil(seg["rps_t15"] / CAPACITY_PER_POD).clip(lower=2, upper=20)

        seg["segment_id"] = seg_id
        valid = seg.dropna(subset=["rps_t5", "rps_t10", "rps_t15"])
        if not valid.empty:
            parts.append(valid)

    if not parts:
        return df.iloc[0:0]
    return pd.concat(parts).sort_index()


def prepare_dataset(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, object]]:
    stale_cols = [c for c in TARGET_COLUMNS + ["segment_id"] if c in df.columns]
    if stale_cols:
        df = df.drop(columns=stale_cols)

    df = df.sort_index()
    for feature_name in QUERIED_FEATURES:
        if feature_name not in df.columns:
            df[feature_name] = np.nan

    df[QUERIED_FEATURES] = df[QUERIED_FEATURES].apply(pd.to_numeric, errors="coerce")
    df, missing_counts, dropped_rows = drop_rows_missing_required_features(df, REQUIRED_QUERY_FEATURES)

    if df.empty:
        raise RuntimeError("No rows remain after enforcing required feature completeness.")

    df = add_temporal_features(df)
    df = add_prediction_targets(df)
    if df.empty:
        raise RuntimeError("No rows remain after creating prediction targets.")

    df.index.name = "timestamp"
    return df, {
        "dropped_incomplete_rows": dropped_rows,
        "missing_required_values": missing_counts,
    }


def build_feature_dataframe(
    hours: int = 168, step: str = "1m", resample: str | None = None
) -> tuple[pd.DataFrame, dict[str, object]]:
    print(f"Collecting {hours}h of data (step={step}) for app: {TARGET_APP}")
    feature_series = {}
    missing_features = []

    for feature_name, query in QUERIES.items():
        print(f"  Fetching {feature_name}...")
        series = collect_range(query, hours=hours, step=step)
        if not series.empty:
            feature_series[feature_name] = series
        else:
            missing_features.append(feature_name)

    if missing_features:
        raise RuntimeError(
            "Required metrics missing from Prometheus: " + ", ".join(sorted(missing_features))
        )

    df = pd.DataFrame(feature_series).sort_index()
    if resample:
        print(f"Resampling data to {resample} intervals (segment-aware)...")
        df = resample_by_segment(df, step_to_pandas_freq(resample), GAP_THRESHOLD_MINUTES)

    prepared, quality_stats = prepare_dataset(df)
    quality_stats["missing_features"] = missing_features
    return prepared, quality_stats


def build_dataset_health(df: pd.DataFrame, quality_stats: dict[str, object]) -> dict[str, object]:
    health = {
        "rows": int(len(df)),
        "features": int(len(df.columns)),
        "queried_features": list(QUERIED_FEATURES),
        "temporal_features": [c for c in FEATURE_COLUMNS if c not in QUERIED_FEATURES],
        "dropped_incomplete_rows": int(quality_stats.get("dropped_incomplete_rows", 0)),
        "missing_required_values": quality_stats.get("missing_required_values", {}),
        "weekend_rows": int(df["is_weekend"].sum()),
        "weekday_rows": int((df["is_weekend"] == 0).sum()),
        "segment_count": 0,
        "max_gap": None,
        "date_range": {"start": None, "end": None},
    }

    if not df.empty:
        seg_ids = _detect_segments(df)
        health["segment_count"] = int(seg_ids.nunique())
        if len(df.index) > 1:
            max_gap = df.index.to_series().diff().dropna().max()
            health["max_gap"] = str(max_gap)
        health["date_range"] = {
            "start": df.index.min().isoformat(),
            "end": df.index.max().isoformat(),
        }

    return health


def write_health_report(output_path: str, health: dict[str, object]) -> str:
    base_path = Path(output_path)
    if base_path.suffix:
        health_path = base_path.with_suffix(".health.json")
    else:
        health_path = base_path.with_name(base_path.name + ".health.json")
    health_path.write_text(json.dumps(health, indent=2) + "\n", encoding="ascii")
    return str(health_path)


def print_dataset_health(health: dict[str, object]) -> None:
    print(f"\n{'=' * 50}")
    print("Dataset Health Report")
    print(f"{'=' * 50}")
    print(f"Total rows       : {health['rows']}")
    print(f"Total features   : {health['features']}")
    print(f"Weekend rows     : {health['weekend_rows']}")
    print(f"Weekday rows     : {health['weekday_rows']}")
    print(f"Dropped rows     : {health['dropped_incomplete_rows']}")
    print(f"Segments         : {health['segment_count']}")
    if health["max_gap"]:
        print(f"Max time gap     : {health['max_gap']}")
    if health["date_range"]["start"]:
        print(f"Date range       : {health['date_range']['start']} -> {health['date_range']['end']}")
    missing_required = health.get("missing_required_values", {})
    if missing_required:
        print("Missing required :")
        for feature_name, count in sorted(missing_required.items()):
            print(f"  - {feature_name}: {count}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export PPA training data from Prometheus.")
    parser.add_argument("--hours", type=int, default=168, help="Hours of data to collect (default: 168)")
    parser.add_argument("--step", type=str, default="1m", help="Prometheus query step (default: 1m, try 15s)")
    parser.add_argument("--resample", type=str, default=None, help="Resample resulting dataframe (e.g., 1m)")
    args = parser.parse_args()

    df, quality_stats = build_feature_dataframe(hours=args.hours, step=args.step, resample=args.resample)
    output_path = os.getenv("OUTPUT_PATH", "data-collection/training-data/training_data.csv")
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    effective_step = args.resample if args.resample else args.step
    round_freq = step_to_pandas_freq(effective_step)

    if os.path.exists(output_path):
        print(f"  Found existing dataset at {output_path}, safely appending new data...")
        df_existing = pd.read_csv(output_path, index_col="timestamp", parse_dates=True)
        df_existing.index = pd.to_datetime(df_existing.index, utc=True)
        df_combined = pd.concat([df_existing, df])
        df_combined.index = pd.to_datetime(df_combined.index, utc=True).round(round_freq)
        df = df_combined[~df_combined.index.duplicated(keep="last")].sort_index()

    df.to_csv(output_path)

    health = build_dataset_health(df, quality_stats)
    health_path = write_health_report(output_path, health)
    print_dataset_health(health)
    print(f"Health report     : {health_path}")
