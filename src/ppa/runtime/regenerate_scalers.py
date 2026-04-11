"""Regenerate scalers inside pod for pickle compatibility.

Usage:
    python regenerate_scalers.py <app_name> <horizon1,horizon2,...> <csv_path>
"""

import logging
import sys
from pathlib import Path

import joblib
import pandas as pd
from sklearn.preprocessing import MinMaxScaler

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

sys.path.insert(0, "/app")

try:
    from ppa.common.feature_spec import FEATURE_COLUMNS
except ImportError:
    FEATURE_COLUMNS = [
        "rps_per_replica",
        "cpu_utilization_pct",
        "memory_utilization_pct",
        "latency_p95_ms",
        "active_connections",
        "error_rate",
        "cpu_acceleration",
        "rps_acceleration",
        "replicas_normalized",
    ]
    log.warning("Using fallback FEATURE_COLUMNS (ppa not in pod)")


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

    features = [c for c in FEATURE_COLUMNS if c in df.columns]
    if len(features) < 3:
        log.error(f"Insufficient features for {horizon}")
        return False

    df_clean = df.dropna(subset=features + [horizon])
    if len(df_clean) < 100:
        log.error(f"Too few samples: {len(df_clean)}")
        return False

    try:
        scaler = MinMaxScaler()
        scaler.fit(df_clean[features].values)

        target_scaler = MinMaxScaler()
        target_scaler.fit(df_clean[[horizon]].values)

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
