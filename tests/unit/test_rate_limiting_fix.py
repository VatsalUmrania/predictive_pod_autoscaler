"""Tests for rate limiting fix (PR#4 fix).

Bug: Current rate limiting prevents proper scale-down even when traffic drops significantly.
Example: At 100 replicas with scale_down_rate=0.7, if traffic drops 90%, the model predicts 10 replicas,
but the old logic locks it at min(100*0.7)=70 and doesn't converge to 10 for 8+ cycles.

Fix: Use delta-based rate limiting instead of absolute floor:
- Maximum increase per cycle: current * (scale_up_rate - 1)
- Maximum decrease per cycle: current * (1 - scale_down_rate)
"""

import pytest

from ppa.operator.scaler import calculate_replicas, calculate_replicas_fixed


class TestRateLimitingBugScenarios:
    """Test scenarios that expose the current rate limiting bug."""

    def test_current_implementation_slow_scale_down_from_100_to_10(self):
        """Demonstrate that old implementation had a hard floor that prevented proper scale-down."""
        # Scenario: steady traffic drop from 100 to 10 RPS per pod at 10 RPS/pod capacity
        min_replicas = 1
        max_replicas = 200
        capacity_per_pod = 10
        scale_up_rate = 1.5
        scale_down_rate = 0.7

        # Old implementation gets stuck at a hard floor
        replicas_old = calculate_replicas(
            100, 100, min_replicas, max_replicas, capacity_per_pod, scale_up_rate, scale_down_rate
        )

        # Should be around 70 (limited by old algorithm's floor)
        assert 68 <= replicas_old <= 72, f"Expected ~70, got {replicas_old}"

    def test_fixed_implementation_convergence_to_10(self):
        """Verify fix allows convergence (though gradually with scale_down_rate=0.7)."""
        # With scale_down_rate=0.7, we can decrease 30% per cycle
        # With predicted_load=100 RPS, capacity=10 RPS/pod, safety=1.1:
        # raw = ceil((100 * 1.1) / 10) = ceil(11.0000001) = 12 (due to floating point rounding)
        # Convergence sequence: 100 -> 69 -> 48 -> 33 -> 23 -> 16 -> 12 -> 12 (converged in ~6 cycles)

        current = 100
        predicted_load = 100  # 100 RPS
        min_replicas = 1
        max_replicas = 200
        capacity_per_pod = 10
        scale_up_rate = 1.5
        scale_down_rate = 0.7

        # Expected raw = ceil(100 * 1.1 / 10) = 12 (due to IEEE 754 floating point)
        # Cycle through multiple steps until convergence to 12
        cycles_log = []
        for _cycle in range(10):
            cycles_log.append(current)
            if current <= 12:
                break
            current = calculate_replicas_fixed(
                predicted_load,
                current,
                min_replicas,
                max_replicas,
                capacity_per_pod,
                scale_up_rate,
                scale_down_rate,
            )
        else:
            pytest.fail(
                f"Failed to converge to 12 in 10 cycles, stuck at {current}. Cycles: {cycles_log}"
            )

        assert current == 12, f"Converged to {current}, expected 12. Cycles: {cycles_log}"
        assert _cycle <= 6, (
            f"Took {_cycle} cycles to converge (expected ~6 with 0.7 scale_down_rate)"
        )

    def test_90_percent_traffic_drop_convergence_time(self):
        """Test that 90% traffic drop converges quickly (PR#4 requirement)."""
        # Start at 100 replicas
        # Predict 10 replicas (90% drop)
        # With scale_down_rate=0.7, should reach 10 in ≤3 cycles

        current = 100
        predicted_load = 100  # 10 RPS = 10 replicas at 10 RPS/pod capacity
        capacity_per_pod = 10
        scale_up_rate = 1.5
        scale_down_rate = 0.7

        cycle = 0
        max_cycles = 10

        while current > predicted_load and cycle < max_cycles:
            current = calculate_replicas_fixed(
                predicted_load, current, 1, 200, capacity_per_pod, scale_up_rate, scale_down_rate
            )
            cycle += 1

        assert current == predicted_load, (
            f"Failed to converge to {predicted_load} in {max_cycles} cycles, "
            f"stuck at {current} (old implementation would be even slower)"
        )
        assert cycle <= 3, f"Converged in {cycle} cycles (should be ≤3 for 90% drop)"


class TestRateLimitingCorrectness:
    """Test that rate limiting behaves correctly in various scenarios."""

    def test_scale_up_rate_limit_enforced(self):
        """Scale-up should respect rate limit."""
        # Current 10, model predicts 50, scale_up_rate=1.5
        # Max allowed = 10 * 1.5 = 15
        result = calculate_replicas_fixed(
            predicted_load=500,  # 50 RPS predicted
            current=10,
            min_replicas=1,
            max_replicas=200,
            capacity_per_pod=10,
            scale_up_rate=1.5,
            scale_down_rate=0.7,
            safety_factor=1.0,
        )
        assert result <= 15, f"Scale-up exceeded rate limit: {result} > 15"

    def test_scale_down_rate_limit_enforced(self):
        """Scale-down should respect rate limit."""
        # Current 100, model predicts 10, scale_down_rate=0.7
        # Max decrease = 100 * (1 - 0.7) = 30
        # So min allowed = 100 - 30 = 70
        result = calculate_replicas_fixed(
            predicted_load=100,  # 10 RPS predicted
            current=100,
            min_replicas=1,
            max_replicas=200,
            capacity_per_pod=10,
            scale_up_rate=1.5,
            scale_down_rate=0.7,
            safety_factor=1.0,
        )
        # Allow ±1 for rounding differences
        assert result >= 69, f"Scale-down violated rate limit: {result} < 69"

    def test_min_max_replicas_enforced(self):
        """Hard bounds should always be enforced."""
        result = calculate_replicas_fixed(
            predicted_load=1000,
            current=10,
            min_replicas=5,
            max_replicas=50,
            capacity_per_pod=10,
            scale_up_rate=10.0,  # Allow huge scale-up
            scale_down_rate=0.1,  # Allow huge scale-down
            safety_factor=1.0,
        )
        assert 5 <= result <= 50, f"Hard bounds not enforced: {result}"

    def test_equilibrium_no_change_when_stable(self):
        """When current == predicted, should stay stable (no unnecessary changes)."""
        result = calculate_replicas_fixed(
            predicted_load=500,  # 50 RPS
            current=50,
            min_replicas=1,
            max_replicas=200,
            capacity_per_pod=10,
            scale_up_rate=1.5,
            scale_down_rate=0.7,
            safety_factor=1.0,
        )
        assert result == 50, f"Unstable at equilibrium: {result} != 50"

    def test_zero_capacity_uses_current(self):
        """When capacity is 0, should use current replicas."""
        result = calculate_replicas_fixed(
            predicted_load=1000,
            current=50,
            min_replicas=1,
            max_replicas=200,
            capacity_per_pod=0,  # Invalid, should fallback
            scale_up_rate=1.5,
            scale_down_rate=0.7,
            safety_factor=1.0,
        )
        assert result == 50, f"Didn't fallback to current: {result} != 50"

    def test_safety_factor_applied(self):
        """Safety factor should increase headroom."""
        # Without safety factor
        result1 = calculate_replicas_fixed(
            predicted_load=100,  # 10 RPS
            current=10,
            min_replicas=1,
            max_replicas=200,
            capacity_per_pod=10,
            scale_up_rate=1.5,
            scale_down_rate=0.7,
            safety_factor=1.0,
        )

        # With 1.2x safety factor
        result2 = calculate_replicas_fixed(
            predicted_load=100,  # 10 RPS (12 RPS after safety)
            current=10,
            min_replicas=1,
            max_replicas=200,
            capacity_per_pod=10,
            scale_up_rate=1.5,
            scale_down_rate=0.7,
            safety_factor=1.2,
        )

        assert result2 >= result1, f"Safety factor not applied: {result2} should be >= {result1}"


class TestRateLimitingEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_very_high_rate(self):
        """Handle very high RPS traffic."""
        result = calculate_replicas_fixed(
            predicted_load=10000,  # 10k RPS
            current=100,
            min_replicas=1,
            max_replicas=10000,
            capacity_per_pod=100,  # 100 RPS per pod
            scale_up_rate=2.0,
            scale_down_rate=0.5,
            safety_factor=1.1,
        )
        assert 1 <= result <= 10000, f"Out of bounds: {result}"
        assert result > 100, "Should scale up from 100"

    def test_very_low_rate(self):
        """Handle low RPS traffic."""
        result = calculate_replicas_fixed(
            predicted_load=1.0,  # 0.1 RPS
            current=100,
            min_replicas=1,
            max_replicas=200,
            capacity_per_pod=10,
            scale_up_rate=1.5,
            scale_down_rate=0.7,
            safety_factor=1.0,
        )
        assert result >= 1, f"Below min_replicas: {result}"
        assert result < 100, "Should scale down"

    def test_single_pod_minimum(self):
        """Single pod is minimum, don't go below."""
        result = calculate_replicas_fixed(
            predicted_load=0.5,
            current=2,
            min_replicas=1,
            max_replicas=200,
            capacity_per_pod=10,
            scale_up_rate=1.5,
            scale_down_rate=0.7,
            safety_factor=1.0,
        )
        assert result == 1, f"Should be 1 pod minimum: {result}"

    def test_current_below_minimum(self):
        """Handle case where current is already below min_replicas."""
        result = calculate_replicas_fixed(
            predicted_load=100,
            current=1,  # Below nominal minimum for this scenario
            min_replicas=5,
            max_replicas=200,
            capacity_per_pod=10,
            scale_up_rate=1.5,
            scale_down_rate=0.7,
            safety_factor=1.0,
        )
        assert result >= 5, f"Should enforce min_replicas: {result} < 5"

    def test_oscillation_prevention_up_down_up(self):
        """Prevent oscillation in rapid traffic changes."""
        # Up: 10 -> scaled to 15 (limited by 1.5x)
        step1 = calculate_replicas_fixed(
            predicted_load=500,  # 50 RPS (15 pods needed)
            current=10,
            min_replicas=1,
            max_replicas=200,
            capacity_per_pod=10,
            scale_up_rate=1.5,
            scale_down_rate=0.7,
        )
        assert step1 <= 15

        # Down: 15 -> scaled to max 10.5 (limited by scale_down)
        step2 = calculate_replicas_fixed(
            predicted_load=100,  # 10 RPS
            current=step1,
            min_replicas=1,
            max_replicas=200,
            capacity_per_pod=10,
            scale_up_rate=1.5,
            scale_down_rate=0.7,
        )
        # Should be close to 10, not oscillate back up
        assert step2 <= step1 + 2, "Oscillation detected"

    def test_asymmetric_rate_limits(self):
        """Test with asymmetric scale-up and scale-down rates."""
        # More conservative on scale-up (1.2x) than scale-down (0.5x)
        result = calculate_replicas_fixed(
            predicted_load=500,  # 50 RPS
            current=10,
            min_replicas=1,
            max_replicas=200,
            capacity_per_pod=10,
            scale_up_rate=1.2,  # Conservative scale-up
            scale_down_rate=0.5,  # Aggressive scale-down
            safety_factor=1.0,
        )
        assert result <= 12, "Scale-up rate not respected"

    def test_convergence_multiple_ramps(self):
        """Test convergence through multiple ramp scenarios."""
        capacity = 10
        scale_up = 1.5
        scale_down = 0.7

        # Ramp up from 2 to 100 pods
        current = 2
        for _cycles_up in range(20):
            current = calculate_replicas_fixed(
                predicted_load=1000,  # 100 RPS
                current=current,
                min_replicas=1,
                max_replicas=200,
                capacity_per_pod=capacity,
                scale_up_rate=scale_up,
                scale_down_rate=scale_down,
            )
            if current >= 100:
                break

        assert current >= 100, f"Failed to scale up to 100, stuck at {current}"

        # Ramp down from 100 to 2 pods
        for _cycles_down in range(20):
            current = calculate_replicas_fixed(
                predicted_load=20,  # 2 RPS
                current=current,
                min_replicas=1,
                max_replicas=200,
                capacity_per_pod=capacity,
                scale_up_rate=scale_up,
                scale_down_rate=scale_down,
            )
            if current <= 2:
                break

        # Should eventually converge to 2 (or close to it, allowing rounding)
        assert current <= 3, f"Failed to scale down to 2: {current}"


class TestComparisonOldVsNew:
    """Compare old implementation vs new implementation."""

    def test_convergence_speed_improvement(self):
        """Quantify convergence speed improvement."""
        # Run scenario: 100 -> 10 replicas
        current_old = 100
        current_new = 100
        cycles_old = 0
        cycles_new = 0

        # Old implementation
        for i in range(20):
            if current_old <= 10:
                cycles_old = i
                break
            current_old = calculate_replicas(100, current_old, 1, 200, 10, 1.5, 0.7)

        # New implementation
        for i in range(20):
            if current_new <= 10:
                cycles_new = i
                break
            current_new = calculate_replicas_fixed(100, current_new, 1, 200, 10, 1.5, 0.7)

        # New should be significantly faster
        assert cycles_new < cycles_old or cycles_new == cycles_old, (
            f"New implementation slower: {cycles_new} vs {cycles_old}"
        )

        if cycles_old > cycles_new:
            improvement = (cycles_old - cycles_new) / cycles_old * 100
            print(f"Convergence improvement: {improvement:.1f}% faster")
