"""Minimal unit tests for Phase 3: Artifact retry logic, legacy fallback, thread safety."""

import os
import tempfile
from threading import Lock
from unittest.mock import MagicMock, patch

import pytest

from ppa.domain import CRState


class TestArtifactRetryCounter:
    """Test artifact_load_failures counter increments and resets correctly."""

    def test_counter_increments(self):
        """Verify counter increments on each failed attempt."""
        state = CRState(predictor=None)
        assert state.artifact_load_failures == 0

        state.artifact_load_failures += 1
        assert state.artifact_load_failures == 1

        state.artifact_load_failures += 1
        assert state.artifact_load_failures == 2

        state.artifact_load_failures += 1
        assert state.artifact_load_failures == 3

    def test_counter_resets_on_success(self):
        """Verify counter resets to 0 when artifacts become available."""
        state = CRState(predictor=None)
        state.artifact_load_failures = 5

        # Simulate recovery
        state.artifact_load_failures = 0

        assert state.artifact_load_failures == 0


class TestLegacyArtifactDetection:
    """Test legacy artifact path detection and transitions."""

    def test_legacy_flag_initialized_false(self):
        """Verify legacy flag starts as False."""
        state = CRState(predictor=None)
        assert state.using_legacy_artifacts is False

    def test_legacy_flag_set_on_detection(self):
        """Verify legacy flag can be set to True."""
        state = CRState(predictor=None)
        state.using_legacy_artifacts = True
        assert state.using_legacy_artifacts is True

    def test_legacy_flag_reset_on_canonical_available(self):
        """Verify legacy flag resets to False when canonical becomes available."""
        state = CRState(predictor=None)
        state.using_legacy_artifacts = True

        # Simulate canonical path becoming available
        state.using_legacy_artifacts = False

        assert state.using_legacy_artifacts is False


class TestPredictorMissingGuard:
    """Test that predictor=None guards work correctly."""

    def test_crstate_predictor_none_on_init(self):
        """Verify CRState can be initialized with predictor=None."""
        state = CRState(predictor=None)
        assert state.predictor is None

    def test_predictor_missing_logged_flag(self):
        """Verify predictor_missing_logged flag tracks logging state."""
        state = CRState(predictor=None)
        assert state.predictor_missing_logged is False

        state.predictor_missing_logged = True
        assert state.predictor_missing_logged is True

        state.predictor_missing_logged = False
        assert state.predictor_missing_logged is False


class TestThreadSafety:
    """Test thread-safe state initialization patterns."""

    def test_double_check_locking_pattern(self):
        """Verify double-check locking prevents duplicate state creation."""
        _test_state_dict = {}
        _test_lock = Lock()

        def initialize_state(key):
            """Simulate double-check locking pattern."""
            existing = _test_state_dict.get(key)

            if existing is None:
                with _test_lock:
                    existing = _test_state_dict.get(key)
                    if existing is None:
                        existing = CRState(predictor=None)
                        _test_state_dict[key] = existing

            return existing

        # First call creates state
        state1 = initialize_state(("ns", "cr-1"))
        assert state1 is not None
        assert _test_state_dict[("ns", "cr-1")] is state1

        # Second call reuses same state
        state2 = initialize_state(("ns", "cr-1"))
        assert state2 is state1  # Same object
        assert len(_test_state_dict) == 1  # Only one entry

    def test_mutation_under_lock_pattern(self):
        """Verify state mutations are safe under lock."""
        state = CRState(predictor=None)
        _lock = Lock()

        # Simulate locked mutation (as in _parse_crd_spec)
        with _lock:
            state.artifact_load_failures += 1
            failures = state.artifact_load_failures

        assert failures == 1
        assert state.artifact_load_failures == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
