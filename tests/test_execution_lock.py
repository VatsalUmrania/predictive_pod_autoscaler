# tests/test_execution_lock.py — Execution Lock + Hysteresis Tests
"""
Tests for the execution lock with hysteresis.

Validates:
- Cooldown enforcement works correctly
- Hysteresis suppresses trivial changes
- CRITICAL signals bypass the lock
- Lock state can be reset
"""

import time

import pytest
from nexus.predictive.execution_lock import (
    ExecutionLock,
    ExecutionLockStore,
)


class TestExecutionLock:
    """Test ExecutionLock dataclass."""

    def test_initial_state(self):
        """Initial state should allow execution."""
        lock = ExecutionLock(
            deployment="test-app",
            namespace="default",
        )
        can_execute, reason = lock.can_execute(10)
        assert can_execute is True
        assert reason == ""

    def test_cooldown_blocks_execution(self):
        """Cooldown should block execution."""
        lock = ExecutionLock(
            deployment="test-app",
            namespace="default",
        )
        lock.record_execution(10, 5)

        can_execute, reason = lock.can_execute(15)
        assert can_execute is False
        assert "cooldown" in reason

    def test_cooldown_allows_after_duration(self):
        """Cooldown should allow execution after duration."""
        lock = ExecutionLock(
            deployment="test-app",
            namespace="default",
            MIN_STABLE_DURATION=1,  # 1 second for testing
        )
        lock.record_execution(10, 5)

        # Should be blocked immediately
        can_execute, _ = lock.can_execute(15)
        assert can_execute is False

        # Wait for cooldown to expire
        time.sleep(1.1)

        # Should be allowed now
        can_execute, _ = lock.can_execute(15)
        assert can_execute is True

    def test_hysteresis_blocks_small_changes(self):
        """Hysteresis should block small changes."""
        lock = ExecutionLock(
            deployment="test-app",
            namespace="default",
        )
        lock.record_execution(10, 5)

        can_execute, reason = lock.can_execute(11)  # delta = 1
        assert can_execute is False
        assert "hysteresis" in reason

    def test_hysteresis_allows_large_changes(self):
        """Hysteresis should allow large changes."""
        lock = ExecutionLock(
            deployment="test-app",
            namespace="default",
        )
        lock.record_execution(10, 5)

        can_execute, _ = lock.can_execute(13)  # delta = 3
        assert can_execute is True

    def test_critical_bypasses_lock(self):
        """CRITICAL signals should bypass the lock."""
        lock = ExecutionLock(
            deployment="test-app",
            namespace="default",
        )
        lock.record_execution(10, 5)

        can_execute, _ = lock.can_execute(15, severity="CRITICAL")
        assert can_execute is True

    def test_emergency_bypasses_lock(self):
        """EMERGENCY signals should bypass the lock."""
        lock = ExecutionLock(
            deployment="test-app",
            namespace="default",
        )
        lock.record_execution(10, 5)

        can_execute, _ = lock.can_execute(15, severity="EMERGENCY")
        assert can_execute is True

    def test_record_execution_updates_state(self):
        """record_execution should update lock state."""
        lock = ExecutionLock(
            deployment="test-app",
            namespace="default",
        )
        lock.record_execution(15, 10)

        assert lock._last_target == 15
        assert lock._last_executed_replicas == 10
        assert lock._last_executed_at is not None

    def test_reset_clears_lock(self):
        """reset should clear the lock state."""
        lock = ExecutionLock(
            deployment="test-app",
            namespace="default",
        )
        lock.record_execution(10, 5)

        lock.reset()

        assert lock._last_executed_at is None
        assert lock._last_target is None
        assert lock._last_executed_replicas is None

    def test_is_critical_override(self):
        """is_critical_override should identify CRITICAL/EMERGENCY."""
        lock = ExecutionLock(
            deployment="test-app",
            namespace="default",
        )

        assert lock.is_critical_override("CRITICAL") is True
        assert lock.is_critical_override("EMERGENCY") is True
        assert lock.is_critical_override("INFO") is False
        assert lock.is_critical_override("WARNING") is False

    def test_get_remaining_cooldown(self):
        """get_remaining_cooldown should return remaining time."""
        lock = ExecutionLock(
            deployment="test-app",
            namespace="default",
            MIN_STABLE_DURATION=10,
        )
        lock.record_execution(10, 5)

        remaining = lock.get_remaining_cooldown()
        assert remaining > 0
        assert remaining <= 10

    def test_get_remaining_cooldown_zero_when_not_in_cooldown(self):
        """get_remaining_cooldown should return 0 when not in cooldown."""
        lock = ExecutionLock(
            deployment="test-app",
            namespace="default",
        )

        remaining = lock.get_remaining_cooldown()
        assert remaining == 0

    def test_to_dict(self):
        """to_dict should return serializable dict."""
        lock = ExecutionLock(
            deployment="test-app",
            namespace="default",
        )
        lock.record_execution(10, 5)

        d = lock.to_dict()
        assert d["deployment"] == "test-app"
        assert d["namespace"] == "default"
        assert d["min_stable_duration"] == 60
        assert d["hysteresis_threshold"] == 2
        assert d["last_target"] == 10
        assert d["last_executed_replicas"] == 5
        assert "remaining_cooldown" in d


class TestExecutionLockStore:
    """Test ExecutionLockStore."""

    def test_get_or_create(self):
        """get_or_create should return lock for deployment."""
        store = ExecutionLockStore()
        lock = store.get_or_create("test-app", "default")
        assert lock.deployment == "test-app"
        assert lock.namespace == "default"

    def test_get_or_create_with_custom_params(self):
        """get_or_create should use custom parameters."""
        store = ExecutionLockStore()
        lock = store.get_or_create(
            "test-app",
            "default",
            min_stable_duration=120,
            hysteresis_threshold=5,
        )
        assert lock.MIN_STABLE_DURATION == 120
        assert lock.HYSTERESIS_THRESHOLD == 5

    def test_get_or_create_returns_same_instance(self):
        """get_or_create should return same instance on subsequent calls."""
        store = ExecutionLockStore()
        lock1 = store.get_or_create("test-app", "default")
        lock2 = store.get_or_create("test-app", "default")
        assert lock1 is lock2

    def test_get_returns_none_for_nonexistent(self):
        """get should return None for non-existent deployment."""
        store = ExecutionLockStore()
        lock = store.get("nonexistent", "default")
        assert lock is None

    def test_remove(self):
        """remove should delete lock from store."""
        store = ExecutionLockStore()
        store.get_or_create("test-app", "default")
        store.remove("test-app", "default")
        lock = store.get("test-app", "default")
        assert lock is None

    def test_all_locks(self):
        """all_locks should return all stored locks."""
        store = ExecutionLockStore()
        store.get_or_create("app1", "default")
        store.get_or_create("app2", "default")
        locks = store.all_locks()
        assert len(locks) == 2
        deployment_names = {lock.deployment for lock in locks}
        assert deployment_names == {"app1", "app2"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
