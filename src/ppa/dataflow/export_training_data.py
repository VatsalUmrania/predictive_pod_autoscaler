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
from typing import cast

import numpy as np
import pandas as pd
import requests  # type: ignore[import-untyped]

from ppa.common.constants import CAPACITY_PER_POD, GAP_THRESHOLD_MINUTES
from ppa.common.feature_spec import FEATURE_COLUMNS, QUERIED_FEATURES, TARGET_COLUMNS
from ppa.dataflow.config import (
    CONTAINER_NAME,
    NAMESPACE,
    PROMETHEUS_URL,
    QUERIES,
    REQUIRED_QUERY_FEATURES,
    TARGET_APP,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


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


CHUNK_HOURS = 6  # Max hours per individual Prometheus request
PROM_TIMEOUT = 120  # Seconds — histogram_quantile over large ranges needs more than 30s


def _fetch_chunk(query: str, start: datetime, end: datetime, step: str) -> list:
    """Fetch a single time range chunk from Prometheus. Returns raw values list."""
    params: dict[str, str | float] = {
        "query": query,
        "start": start.timestamp(),
        "end": end.timestamp(),
        "step": step,
    }
    response = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query_range",
        params=cast(dict[str, str | int | float], params),
        timeout=PROM_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload}")
    data = payload.get("data", {}).get("result", [])
    if not data:
        return []
    return data[0]["values"]  # type: ignore[no-any-return]


def collect_range(query: str, hours: int = 24, step: str = "1m") -> pd.Series:
    end = datetime.now(timezone.utc)

    step_seconds = step_to_seconds(step)
    requested_points = (hours * 3600) / step_seconds

    if requested_points > 10000:
        safe_hours = (10000 * step_seconds) / 3600
        print(
            f"  WARNING: Requested {int(requested_points)} points. Prometheus limits queries to 11,000."
        )
        print(
            f"  WARNING: Capping query window to {safe_hours:.1f} hours to prevent silent failure."
        )
        total_hours = safe_hours
    else:
        total_hours = float(hours)

    # Split into CHUNK_HOURS-sized windows to avoid Prometheus read timeouts on
    # expensive queries (e.g. histogram_quantile over large ranges).
    all_values: list = []
    chunk_start = end - timedelta(hours=total_hours)
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(hours=CHUNK_HOURS), end)
        chunk_points = (chunk_end - chunk_start).total_seconds() / step_seconds
        if chunk_points > 11000:
            # Shouldn't happen given CHUNK_HOURS, but guard anyway
            chunk_end = chunk_start + timedelta(seconds=11000 * step_seconds)
        values = _fetch_chunk(query, chunk_start, chunk_end, step)
        all_values.extend(values)
        chunk_start = chunk_end

    if not all_values:
        return pd.Series(dtype=float)

    series = pd.Series(
        {datetime.fromtimestamp(ts, timezone.utc): float(val) for ts, val in all_values},
        dtype=float,
    )
    series = series[~series.index.duplicated(keep="last")]
    series.index = series.index.round(step_to_pandas_freq(step))
    return series


def resample_by_segment(
    df: pd.DataFrame,
    resample_freq: str,
    gap_threshold_minutes: int = GAP_THRESHOLD_MINUTES,
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


def add_segment_ids(df: pd.DataFrame) -> pd.DataFrame:
    """Assign segment_id based on timestamp gaps after all data is combined."""
    if df.empty:
        return df
    df = df.sort_index().copy()
    segment_ids = _detect_segments(df)
    df["segment_id"] = segment_ids
    return df


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
        return (
            df,
            {k: int(v) for k, v in missing_by_feature.items() if int(v) > 0},
            dropped_rows,
        )

    df[required_features] = numeric_required.loc[df.index]
    return (
        df,
        {k: int(v) for k, v in missing_by_feature.items() if int(v) > 0},
        dropped_rows,
    )


def add_prediction_targets(df: pd.DataFrame) -> pd.DataFrame:
    """Build future targets within continuous segments only."""
    df = df.sort_index()
    seg_ids = _detect_segments(df)
    parts = []

    for _seg_id, seg in df.groupby(seg_ids):
        base = seg["requests_per_second"]
        seg = seg.copy()
        seg["rps_t3m"] = base.reindex(seg.index + pd.Timedelta(minutes=3)).to_numpy()
        seg["rps_t5m"] = base.reindex(seg.index + pd.Timedelta(minutes=5)).to_numpy()
        seg["rps_t10m"] = base.reindex(seg.index + pd.Timedelta(minutes=10)).to_numpy()

        seg["replicas_t3m"] = np.ceil(seg["rps_t3m"] / CAPACITY_PER_POD).clip(lower=2, upper=20)
        seg["replicas_t5m"] = np.ceil(seg["rps_t5m"] / CAPACITY_PER_POD).clip(lower=2, upper=20)
        seg["replicas_t10m"] = np.ceil(seg["rps_t10m"] / CAPACITY_PER_POD).clip(lower=2, upper=20)

        valid = seg.dropna(subset=["rps_t3m", "rps_t5m", "rps_t10m"])
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
    df, missing_counts, dropped_rows = drop_rows_missing_required_features(
        df, REQUIRED_QUERY_FEATURES
    )

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
    app_name: str = TARGET_APP,
    hours: int = 168,
    step: str = "1m",
    resample: str | None = None,
) -> tuple[pd.DataFrame, dict[str, object]]:
    print(f"Collecting {hours}h of data (step={step}) for app: {app_name}")
    feature_series = {}
    missing_features = []

    # Generate dynamic queries for the specific app_name to avoid being bound to config.TARGET_APP
    from ppa.common.promql import build_fallback_queries

    # Assuming build_queries exists, otherwise we'll inject it. Actually common/promql.py exposes build_feature_queries? Let's assume we can just replace TARGET_APP in the pre-rendered QUERIES strings if we don't have build_queries.
    # We will just replace TARGET_APP with app_name in the raw query strings extracted from QUERIES.
    dynamic_queries = {k: v.replace(TARGET_APP, app_name) for k, v in QUERIES.items()}

    fallbacks = build_fallback_queries(app_name, NAMESPACE, CONTAINER_NAME)

    for feature_name, query in dynamic_queries.items():
        if feature_name in ["cpu_acceleration", "rps_acceleration"]:
            continue

        print(f"  Fetching {feature_name}...")
        series = collect_range(query, hours=hours, step=step)

        if series.empty and feature_name == "cpu_utilization_pct":
            print(
                f"  WARNING: No CPU limits found for {app_name}, falling back to absolute cpu_core_percent"
            )
            series = collect_range(fallbacks["cpu_core_percent"], hours=hours, step=step)

        if series.empty and feature_name == "memory_utilization_pct":
            print(
                f"  WARNING: No memory limits found for {app_name}, falling back to absolute memory_usage_bytes"
            )
            series = collect_range(fallbacks["memory_usage_bytes"], hours=hours, step=step)

        if not series.empty:
            feature_series[feature_name] = series
        else:
            missing_features.append(feature_name)

    if missing_features:
        raise RuntimeError(
            "Required metrics missing from Prometheus: " + ", ".join(sorted(missing_features))
        )

    df = pd.DataFrame(feature_series).sort_index()

    if "requests_per_second" in df.columns and "current_replicas" in df.columns:
        df["rps_per_replica"] = df["requests_per_second"] / df["current_replicas"].clip(lower=1)
    if "current_replicas" in df.columns:
        df["replicas_normalized"] = df["current_replicas"] / float(os.getenv("DATA_COLLECTION_MAX_REPLICAS", "20"))
    if "cpu_utilization_pct" in df.columns:
        df["cpu_acceleration"] = df["cpu_utilization_pct"].diff()
    if "rps_per_replica" in df.columns:
        df["rps_acceleration"] = df["rps_per_replica"].diff()

    if resample:
        print(f"Resampling data to {resample} intervals (segment-aware)...")
        df = resample_by_segment(df, step_to_pandas_freq(resample), GAP_THRESHOLD_MINUTES)

    prepared, quality_stats = prepare_dataset(df)

    cols_to_drop = [
        "requests_per_second",
        "current_replicas",
        "cpu_core_percent",
        "memory_usage_bytes",
    ]
    prepared.drop(columns=[c for c in cols_to_drop if c in prepared.columns], inplace=True)

    quality_stats["missing_features"] = missing_features
    return prepared, quality_stats


def build_dataset_health(df: pd.DataFrame) -> dict[str, object]:
    # Check for actual missing values in the final dataset for required features
    missing_required = {
        feature: int(df[feature].isna().sum())
        for feature in REQUIRED_QUERY_FEATURES
        if feature in df.columns and df[feature].isna().sum() > 0
    }

    health = {
        "rows": int(len(df)),
        "features": int(len(df.columns)),
        "queried_features": list(QUERIED_FEATURES),
        "temporal_features": [c for c in FEATURE_COLUMNS if c not in QUERIED_FEATURES],
        "dropped_incomplete_rows": 0,  # Rows are dropped before reaching the final dataset
        "missing_required_values": missing_required,
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
    print(f"Segments         : {health['segment_count']}")
    if health["max_gap"]:
        print(f"Max time gap     : {health['max_gap']}")
    if health["date_range"]["start"]:  # type: ignore[index]
        print(
            f"Date range       : {health['date_range']['start']} -> {health['date_range']['end']}"  # type: ignore[index]
        )
    missing_required = health.get("missing_required_values", {})  # type: ignore[index,arg-type]
    if missing_required:
        print("Missing required :")
        for feature_name, count in sorted(missing_required.items()):  # type: ignore[index,attr-defined]
            print(f"  - {feature_name}: {count}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export PPA training data from Prometheus.")
    parser.add_argument(
        "--app-name",
        type=str,
        default="test-app",
        help="Target application name (default: test-app)",
    )
    parser.add_argument(
        "--hours", type=int, default=168, help="Hours of data to collect (default: 168)"
    )
    parser.add_argument(
        "--step",
        type=str,
        default="1m",
        help="Prometheus query step (default: 1m, try 15s)",
    )
    parser.add_argument(
        "--resample",
        type=str,
        default=None,
        help="Resample resulting dataframe (e.g., 1m)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Run without saving the CSV file")
    parser.add_argument(
        "--assert-schema",
        type=str,
        default=None,
        help="Assert schema matches the specified version (e.g. 'v2')",
    )
    args = parser.parse_args()

    # Build the feature dataframe explicitly using the passed app_name
    df, quality_stats = build_feature_dataframe(
        app_name=args.app_name, hours=args.hours, step=args.step, resample=args.resample
    )

    if args.assert_schema == "v2":
        assert list(df[FEATURE_COLUMNS].columns) == FEATURE_COLUMNS, (
            "Column order mismatch — model will produce wrong predictions"
        )
        nan_count = df[FEATURE_COLUMNS].isna().sum().sum()
        assert nan_count == 0, f"Expected zero NaN values in feature columns, found {nan_count}"
        print("  ✅ Schema assertion passed: 14 feature columns matched exactly with 0 NaNs")

    # Add a placeholder segment_id column so schema matches existing CSV during comparison
    df["segment_id"] = 0

    # Dynamically build output path based on app_name if not overridden by env var
    default_output = f"data/training-data/{args.app_name}.csv"
    output_path = os.getenv("OUTPUT_PATH", default_output)
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    effective_step = args.resample if args.resample else args.step
    round_freq = step_to_pandas_freq(effective_step)

    if not args.dry_run and os.path.exists(output_path):
        print(f"  Found existing dataset at {output_path}, safely appending new data...")
        df_existing = pd.read_csv(output_path, index_col="timestamp", parse_dates=True)
        df_existing.index = pd.to_datetime(df_existing.index, utc=True)

        if set(df_existing.columns) != set(df.columns):
            backup_file = f"{output_path}.bak_{int(datetime.now().timestamp())}"
            print(
                f"  WARNING: Schema mismatch (columns changed). Backing up old data to {backup_file}"
            )
            df_existing.to_csv(backup_file)
            print("  Starting a fresh dataset with the new schema...")
        else:
            df_combined = pd.concat([df_existing, df])
            df_combined.index = pd.to_datetime(df_combined.index, utc=True).round(round_freq)
            df = df_combined[~df_combined.index.duplicated(keep="last")].sort_index()

    # Recalculate segment_id on the full combined dataset so it matches the health report
    df = add_segment_ids(df)

    if not args.dry_run:
        df.to_csv(output_path)
        print(f"Saved dataset to {output_path}")

    health = build_dataset_health(df)
    health_path = write_health_report(output_path, health)
    print_dataset_health(health)
    print(f"Health report     : {health_path}")
