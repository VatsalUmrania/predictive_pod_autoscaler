"""Regression tests for collector-side data quality gates."""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_COLLECTION_DIR = ROOT_DIR / "data-collection"
for path in (ROOT_DIR, DATA_COLLECTION_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

spec = importlib.util.spec_from_file_location(
    "export_training_data",
    DATA_COLLECTION_DIR / "export_training_data.py",
)
export_training_data = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export_training_data)


def test_drop_rows_missing_required_features():
    idx = pd.date_range("2026-03-01T00:00:00Z", periods=3, freq="1min")
    df = pd.DataFrame(
        {
            "requests_per_second": [1.0, 2.0, 3.0],
            "current_replicas": [2.0, np.nan, 2.0],
        },
        index=idx,
    )

    pruned, missing_counts, dropped = export_training_data.drop_rows_missing_required_features(
        df,
        ["requests_per_second", "current_replicas"],
    )

    assert dropped == 1
    assert len(pruned) == 2
    assert missing_counts == {"current_replicas": 1}
