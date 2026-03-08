import os
import sys
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.constants import GAP_THRESHOLD_MINUTES
from common.feature_spec import FEATURE_COLUMNS, TARGET_COLUMNS


def validate(csv_path: str) -> bool:
    print(f"\nrunning validation on {csv_path}...\n")
    if not os.path.exists(csv_path):
        print(f"File not found: {csv_path}")
        return False

    df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
    print(f"Loaded {len(df)} rows.")

    errors = []
    warnings = []

    if len(df) < 50:
        errors.append(f"Insufficient rows: {len(df)}, need at least 50 for testing.")
    elif len(df) < 10000:
        warnings.append(f"Low row count: {len(df)}, target is 10,000 for training.")

    missing_columns = [col for col in FEATURE_COLUMNS + TARGET_COLUMNS[:3] if col not in df.columns]
    if missing_columns:
        errors.append("Missing required columns: " + ", ".join(missing_columns))

    nan_ratio = df.isna().sum() / len(df)
    for col, ratio in nan_ratio.items():
        if ratio > 0.05:
            errors.append(f"High NaN ratio in {col}: {ratio:.2%}")

    excluded_columns = {"segment_id"}

    for col in df.columns:
        if col in excluded_columns:
            continue
        if pd.api.types.is_numeric_dtype(df[col]) and df[col].std() < 1e-4:
            errors.append(f"Zero or near-zero variance in {col}.")

    corr = df.corr(numeric_only=True).abs()
    for i in range(len(corr.columns)):
        for j in range(i + 1, len(corr.columns)):
            col1, col2 = corr.columns[i], corr.columns[j]
            if col1 in excluded_columns or col2 in excluded_columns:
                continue
            if ("rps_t" in col1 and "replicas_t" in col2) or ("rps_t" in col2 and "replicas_t" in col1):
                continue
            if "sin" in col1 or "cos" in col1 or "sin" in col2 or "cos" in col2:
                continue

            cor_val = getattr(corr.iloc[i, j], "item", lambda: corr.iloc[i, j])()
            if cor_val > 0.98:
                errors.append(f"High correlation (>0.98) between {col1} and {col2}: {cor_val:.3f}")

    for target_name in TARGET_COLUMNS[:3]:
        if target_name not in df.columns:
            errors.append(f"Target column missing: {target_name}")

    if isinstance(df.index, pd.DatetimeIndex) and len(df.index) > 1:
        max_gap = df.index.to_series().sort_values().diff().dropna().max()
        if max_gap > pd.Timedelta(minutes=GAP_THRESHOLD_MINUTES):
            warnings.append(
                f"Largest timestamp gap is {max_gap}, exceeds {GAP_THRESHOLD_MINUTES} minutes."
            )

    if warnings:
        for warning in warnings:
            print(f"  WARNING: {warning}")

    if errors:
        print("\nValidation failed:")
        for err in errors:
            print(f"  - {err}")
        return False

    print("\nValidation passed!")
    return True


if __name__ == "__main__":
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "data-collection/training-data/training_data_v2.csv"
    if not validate(csv_path):
        sys.exit(1)
