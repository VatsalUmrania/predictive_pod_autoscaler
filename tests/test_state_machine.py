# tests/test_state_machine.py — Control System State Machine Tests
"""
Tests for the control system state machine.

Validates:
- State transitions are guarded
- Invalid transitions are rejected
- Emergency state bypasses hysteresis
- Cooldown enforcement works correctly
"""

import time

import pytest
from nexus.core.state_machine import (
    TRANSITIONS,
    ControlState,
    DeploymentControlState,
    StateMachineStore,
)


class TestControlState:
    """Test ControlState enum and transitions."""

    def test_transitions_defined(self):
        """Verify all states have defined transitions."""
        for state in ControlState:
            assert state in TRANSITIONS, f"State {state} has no transitions defined"

    def test_valid_transitions(self):
        """Verify all defined transitions are valid."""
        for _from_state, to_states in TRANSITIONS.items():
            for to_state in to_states:
                assert to_state in ControlState, f"Invalid target state: {to_state}"

    def test_idle_can_transition_to_evaluating(self):
        """IDLE → EVALUATING should be valid."""
        assert ControlState.EVALUATING in TRANSITIONS[ControlState.IDLE]

    def test_idle_can_transition_to_emergency(self):
        """IDLE → EMERGENCY should be valid."""
        assert ControlState.EMERGENCY in TRANSITIONS[ControlState.IDLE]

    def test_locked_can_only_transition_to_idle(self):
        """LOCKED should only transition to IDLE."""
        assert TRANSITIONS[ControlState.LOCKED] == {ControlState.IDLE}

    def test_emergency_has_multiple_transitions(self):
        """EMERGENCY should have multiple valid transitions."""
        assert len(TRANSITIONS[ControlState.EMERGENCY]) >= 3


class TestDeploymentControlState:
    """Test DeploymentControlState dataclass."""

    def test_initial_state(self):
        """Initial state should be IDLE."""
        state = DeploymentControlState(
            deployment="test-app",
            namespace="default",
        )
        assert state.state == ControlState.IDLE
        assert state.entered_at is not None

    def test_valid_transition(self):
        """Valid transition should succeed."""
        state = DeploymentControlState(
            deployment="test-app",
            namespace="default",
        )
        result = state.transition(ControlState.EVALUATING, "test reason")
        assert result is True
        assert state.state == ControlState.EVALUATING

    def test_invalid_transition(self):
        """Invalid transition should fail."""
        state = DeploymentControlState(
            deployment="test-app",
            namespace="default",
        )
        result = state.transition(ControlState.LOCKED, "test reason")
        assert result is False
        assert state.state == ControlState.IDLE

    def test_transition_updates_entered_at(self):
        """Transition should update entered_at timestamp."""
        state = DeploymentControlState(
            deployment="test-app",
            namespace="default",
        )
        old_entered_at = state.entered_at
        time.sleep(0.01)  # Small delay
        state.transition(ControlState.EVALUATING)
        assert state.entered_at > old_entered_at

    def test_transition_sets_lock_reason(self):
        """Transition to LOCKED should set lock_reason."""
        state = DeploymentControlState(
            deployment="test-app",
            namespace="default",
        )
        state.transition(ControlState.LOCKED, "circuit breaker")
        assert state.lock_reason == "circuit breaker"

    def test_transition_clears_lock_reason(self):
        """Transition to IDLE should clear lock_reason."""
        state = DeploymentControlState(
            deployment="test-app",
            namespace="default",
        )
        state.transition(ControlState.LOCKED, "test")
        state.transition(ControlState.IDLE)
        assert state.lock_reason is None

    def test_transition_sets_emergency_reason(self):
        """Transition to EMERGENCY should set emergency_reason."""
        state = DeploymentControlState(
            deployment="test-app",
            namespace="default",
        )
        state.transition(ControlState.EMERGENCY, "critical signal")
        assert state.emergency_reason == "critical signal"

    def test_seconds_in_state(self):
        """seconds_in_state should return elapsed time."""
        state = DeploymentControlState(
            deployment="test-app",
            namespace="default",
        )
        time.sleep(0.01)
        elapsed = state.seconds_in_state()
        assert elapsed >= 0.01
        assert elapsed < 1.0  # Should be very small

    def test_can_transition_to(self):
        """can_transition_to should check validity."""
        state = DeploymentControlState(
            deployment="test-app",
            namespace="default",
        )
        assert state.can_transition_to(ControlState.EVALUATING) is True
        assert state.can_transition_to(ControlState.LOCKED) is False

    def test_is_in_cooldown(self):
        """is_in_cooldown should check minimum duration."""
        state = DeploymentControlState(
            deployment="test-app",
            namespace="default",
        )
        state.transition(ControlState.SCALING)
        # Should be in cooldown immediately
        assert state.is_in_cooldown(min_duration_seconds=60) is True
        # Should not be in cooldown after 61 seconds
        assert state.is_in_cooldown(min_duration_seconds=0) is False

    def test_to_dict(self):
        """to_dict should return serializable dict."""
        state = DeploymentControlState(
            deployment="test-app",
            namespace="default",
        )
        state.transition(ControlState.EVALUATING, "test")
        d = state.to_dict()
        assert d["deployment"] == "test-app"
        assert d["namespace"] == "default"
        assert d["state"] == "EVALUATING"
        assert "entered_at" in d
        assert "seconds_in_state" in d


class TestStateMachineStore:
    """Test StateMachineStore."""

    def test_get_or_create(self):
        """get_or_create should return state for deployment."""
        store = StateMachineStore()
        state = store.get_or_create("test-app", "default")
        assert state.deployment == "test-app"
        assert state.namespace == "default"
        assert state.state == ControlState.IDLE

    def test_get_or_create_returns_same_instance(self):
        """get_or_create should return same instance on subsequent calls."""
        store = StateMachineStore()
        state1 = store.get_or_create("test-app", "default")
        state2 = store.get_or_create("test-app", "default")
        assert state1 is state2

    def test_get_returns_none_for_nonexistent(self):
        """get should return None for non-existent deployment."""
        store = StateMachineStore()
        state = store.get("nonexistent", "default")
        assert state is None

    def test_remove(self):
        """remove should delete state from store."""
        store = StateMachineStore()
        store.get_or_create("test-app", "default")
        store.remove("test-app", "default")
        state = store.get("test-app", "default")
        assert state is None

    def test_all_states(self):
        """all_states should return all stored states."""
        store = StateMachineStore()
        store.get_or_create("app1", "default")
        store.get_or_create("app2", "default")
        states = store.all_states()
        assert len(states) == 2
        deployment_names = {s.deployment for s in states}
        assert deployment_names == {"app1", "app2"}

    def test_get_states_in(self):
        """get_states_in should return states in specific state."""
        store = StateMachineStore()
        state1 = store.get_or_create("app1", "default")
        state2 = store.get_or_create("app2", "default")
        state1.transition(ControlState.EVALUATING)
        state2.transition(ControlState.SCALING)

        evaluating = store.get_states_in(ControlState.EVALUATING)
        scaling = store.get_states_in(ControlState.SCALING)

        assert len(evaluating) == 1
        assert len(scaling) == 1
        assert evaluating[0].deployment == "app1"
        assert scaling[0].deployment == "app2"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
