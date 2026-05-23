"""Unit tests for data collection module."""

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from ppa.dataflow import export_training_data
from ppa.dataflow.export_training_data import (
    drop_rows_missing_required_features,
    step_to_pandas_freq,
    step_to_seconds,
)


class TestStepConversion:
    def test_step_to_seconds_seconds(self):
        assert step_to_seconds("30s") == 30
        assert step_to_seconds("15s") == 15

    def test_step_to_seconds_minutes(self):
        assert step_to_seconds("1m") == 60
        assert step_to_seconds("5m") == 300

    def test_step_to_pandas_freq(self):
        assert step_to_pandas_freq("30s") == "30s"
        assert step_to_pandas_freq("1m") == "1min"
        assert step_to_pandas_freq("5m") == "5min"

    def test_align_query_window_snaps_end_to_step_boundary(self):
        end = datetime(2026, 4, 1, 12, 0, 7, tzinfo=timezone.utc)

        start, aligned_end = export_training_data._align_query_window(1, "15s", end=end)

        assert aligned_end == datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert start == datetime(2026, 4, 1, 11, 0, 0, tzinfo=timezone.utc)


class TestDataQuality:
    def test_drop_rows_missing_required_features(self):
        df = pd.DataFrame(
            {
                "rps_per_replica": [10.0, np.nan, 20.0],
                "cpu_utilization_pct": [50.0, 60.0, np.nan],
            }
        )
        required = ["rps_per_replica", "cpu_utilization_pct"]

        result_df, missing, dropped = drop_rows_missing_required_features(df, required)

        assert dropped == 2
        assert len(result_df) == 1

    def test_align_series_to_expected_index_zero_fills_boundary_gaps(self):
        expected_index = pd.date_range("2026-04-01T00:00:00Z", periods=5, freq="15s")
        sparse = pd.Series(
            [1.0, 2.0],
            index=pd.DatetimeIndex(expected_index[-2:]),
            dtype=float,
        )

        aligned = export_training_data._align_series_to_expected_index(
            sparse,
            expected_index,
            "requests_per_second",
        )

        assert list(aligned) == [0.0, 0.0, 0.0, 1.0, 2.0]

    def test_build_feature_dataframe_reuses_one_query_window(self, monkeypatch):
        queries = {
            "requests_per_second": "rps",
            "cpu_utilization_pct": "cpu",
            "memory_utilization_pct": "mem",
            "latency_p95_ms": "lat",
            "active_connections": "conn",
            "error_rate": "err",
            "current_replicas": "rep",
        }
        idx = pd.date_range("2026-04-01T00:00:00Z", periods=3, freq="15s")
        recorded_windows: list[tuple[pd.Timestamp | None, pd.Timestamp | None]] = []

        monkeypatch.setattr(export_training_data, "QUERIES", queries)
        monkeypatch.setattr(
            export_training_data,
            "prepare_dataset",
            lambda df: (df, {"dropped_incomplete_rows": 0, "missing_required_values": {}}),
        )

        def fake_collect_range(query, hours=24, step="1m", *, start=None, end=None):
            recorded_windows.append((pd.Timestamp(start), pd.Timestamp(end)))
            values = {
                "rps": 10.0,
                "cpu": 50.0,
                "mem": 60.0,
                "lat": 20.0,
                "conn": 5.0,
                "err": 0.1,
                "rep": 2.0,
            }
            return pd.Series(values[query], index=idx, dtype=float)

        monkeypatch.setattr(export_training_data, "collect_range", fake_collect_range)

        df, quality = export_training_data.build_feature_dataframe(
            app_name="test-app",
            hours=1,
            step="15s",
        )

        assert not df.empty
        assert quality["missing_features"] == []
        assert len(recorded_windows) == len(queries)
        assert len(set(recorded_windows)) == 1
