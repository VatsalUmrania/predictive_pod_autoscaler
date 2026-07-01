"""Regenerate scalers inside pod for pickle compatibility.

Usage:
    python regenerate_scalers.py <app_name> <horizon1,horizon2,...> <csv_path>
"""

import logging
import re
import sys
from pathlib import Path

import joblib
import pandas as pd
from sklearn.preprocessing import MinMaxScaler

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

sys.path.insert(0, "/app")

try:
    from ppa.common.feature_spec import FEATURE_COLUMNS, TARGET_COLUMNS
except ImportError:
    FEATURE_COLUMNS = [
        "normalized_rps",
        "cpu_utilization_pct",
        "memory_utilization_pct",
        "latency_normalized",
        "error_rate",
        "rps_acceleration",
        "cpu_acceleration",
    ]
    TARGET_COLUMNS = [
        "normalized_rps_t3m",
        "normalized_rps_t5m",
        "normalized_rps_t10m",
    ]
    log.warning("Using fallback FEATURE_COLUMNS/TARGET_COLUMNS (ppa not in pod)")


def _resolve_target_column(horizon: str, df_columns: list[str]) -> str | None:
    """Map an arbitrary horizon label to an actual CSV target column.

    Handles cases like:
      - "normalized_rps_t3m"      → direct match (new schema)
      - "ppa-normalized-rps-t3m" → normalise then suffix-match
      - "rps_t3m"                 → direct match (old schema)

    Schema-agnostic: if the known TARGET_COLUMNS aren't in the CSV
    (e.g. CSV still uses old naming like rps_t3m instead of
    normalized_rps_t3m), falls back to scanning all CSV columns for
    any column ending with the same time suffix (_t3m, _t5m, _t10m).
    """
    # 1. Direct match — horizon is already a valid column name
    if horizon in df_columns:
        return horizon

    # 2. Normalise separators (e.g. "ppa-normalized-rps-t3m" → "ppa_normalized_rps_t3m")
    normalised = horizon.replace("-", "_")
    if normalised in df_columns:
        return normalised

    # 3. Try known TARGET_COLUMNS that are present in this CSV
    for col in TARGET_COLUMNS:
        if col in df_columns:
            suffix = col.split("_")[-1]  # "t3m", "t5m", "t10m"
            if normalised == col or normalised.endswith(f"_{suffix}"):
                return col

    # 4. Fallback: extract time suffix from the horizon string and scan the
    #    CSV columns directly — handles old-schema CSVs where target columns
    #    are named differently (e.g. "rps_t3m" instead of "normalized_rps_t3m")
    m = re.search(r"_(t\d+m)$", normalised)
    if m:
        suffix = m.group(1)  # e.g. "t3m"
        candidates = [c for c in df_columns if c.endswith(f"_{suffix}")]
        if candidates:
            # Prefer RPS-related column; otherwise take first match
            rps = [c for c in candidates if "rps" in c]
            chosen = rps[0] if rps else candidates[0]
            log.warning(
                f"Target column '{horizon}' not found by name; "
                f"using '{chosen}' (suffix=_{suffix}) from CSV columns."
            )
            return chosen

    return None


def regenerate_all(app_name: str, horizons: list[str], csv_path: str) -> dict:
    """Regenerate scalers for all horizons in ONE call."""
    results = {}

    if not Path(csv_path).exists():
        log.error(f"CSV not found: {csv_path}")
        return {h: False for h in horizons}

    try:
        df = pd.read_csv(csv_path)
        log.info(f"Loaded CSV: {len(df)} rows")
    except Exception as e:
        log.error(f"Failed to read CSV: {e}")
        return {h: False for h in horizons}

    for horizon in horizons:
        results[horizon] = _regenerate_single(app_name, horizon, df)

    return results


def _get_model_feature_count(tflite_path: Path) -> int | None:
    """Read the TFLite model's input shape and return the feature count (last dim)."""
    try:
        try:
            from ai_edge_litert.interpreter import Interpreter
        except ImportError:
            from tflite_runtime.interpreter import Interpreter  # type: ignore

        interp = Interpreter(model_path=str(tflite_path))
        interp.allocate_tensors()
        shape = interp.get_input_details()[0]["shape"]  # e.g. [1, 60, 14]
        num_features = int(shape[-1])
        log.info(f"Model input shape: {list(shape)} → {num_features} features")
        return num_features
    except Exception as e:
        log.warning(f"Could not inspect TFLite model {tflite_path}: {e}")
        return None


def _regenerate_single(app_name: str, horizon: str, df: pd.DataFrame) -> bool:
    """Regenerate scalers for a single horizon."""
    # Try both directory structures:
    # 1. Canonical: /models/{app_name}/{horizon}/ppa_model.tflite
    # 2. Legacy: /models/{horizon}/ppa_model.tflite
    base_dir = Path(f"/models/{horizon}")
    tflite = base_dir / "ppa_model.tflite"

    if not tflite.exists():
        # Try canonical structure
        base_dir = Path(f"/models/{app_name}/{horizon}")
        tflite = base_dir / "ppa_model.tflite"
        if not tflite.exists():
            log.warning(f"Skipping {horizon}: no .tflite found at {base_dir}")
            return False

    base_dir.mkdir(parents=True, exist_ok=True)

    # --- Determine the exact feature set from the TFLite model's input shape ---
    num_model_features = _get_model_feature_count(tflite)
    if num_model_features is None:
        log.error(f"Could not read input shape from {tflite}")
        return False

    # Columns that are targets, metadata, or future-look-ahead — never features
    non_feature_cols = set(TARGET_COLUMNS) | {
        "timestamp",
        "segment_id",
        "rps_t3m",
        "rps_t5m",
        "rps_t10m",
        "replicas_t3m",
        "replicas_t5m",
        "replicas_t10m",
        "normalized_rps_t3m",
        "normalized_rps_t5m",
        "normalized_rps_t10m",
        "rolling_max_rps",
        "rolling_max_latency",
    }

    # Priority 1: use known FEATURE_COLUMNS that are present in the CSV
    known_present = [c for c in FEATURE_COLUMNS if c in df.columns]

    # Priority 2: use any CSV column not in the exclusion list
    candidate_cols = [c for c in df.columns if c not in non_feature_cols]

    if len(known_present) == num_model_features:
        features = known_present
        log.info(f"Using {len(features)} known FEATURE_COLUMNS for {horizon}")
    elif len(candidate_cols) >= num_model_features:
        # Take the first N candidate columns — matches the column order used at training
        features = candidate_cols[:num_model_features]
        log.warning(
            f"Model expects {num_model_features} features but FEATURE_COLUMNS has "
            f"{len(FEATURE_COLUMNS)} ({len(known_present)} in CSV). "
            f"Using first {num_model_features} non-target CSV columns: {features}"
        )
    else:
        log.error(
            f"Not enough feature columns in CSV for {horizon}: "
            f"model needs {num_model_features}, found {len(candidate_cols)} candidates"
        )
        return False

    target_col = _resolve_target_column(horizon, list(df.columns))
    if target_col is None:
        log.error(
            f"Cannot find a target column for horizon '{horizon}' in CSV. "
            f"Available columns: {list(df.columns)}"
        )
        return False

    df_clean = df.dropna(subset=features + [target_col])
    if len(df_clean) < 100:
        log.error(f"Too few samples: {len(df_clean)}")
        return False

    try:
        scaler = MinMaxScaler()
        scaler.fit(df_clean[features].values)

        target_scaler = MinMaxScaler()
        target_scaler.fit(df_clean[[target_col]].values)

        # Save with consistent names (scaler.pkl, target_scaler.pkl)
        joblib.dump(scaler, base_dir / "scaler.pkl", protocol=2)
        joblib.dump(target_scaler, base_dir / "target_scaler.pkl", protocol=2)

        log.info(f"OK {horizon}: scalers saved")
        return True

    except Exception as e:
        log.error(f"Failed {horizon}: {e}")
        return False


if __name__ == "__main__":
    app_name = sys.argv[1]
    horizons = sys.argv[2].split(",")
    csv_path = sys.argv[3]

    results = regenerate_all(app_name, horizons, csv_path)

    success_count = sum(results.values())
    total = len(results)
    log.info(f"Done: {success_count}/{total} horizons succeeded")

    sys.exit(0 if success_count == total else 1)
