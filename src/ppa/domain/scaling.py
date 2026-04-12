"""Pure domain logic for replica scaling calculations.

Pure math without Kubernetes dependencies—tests are fast and deterministic.
Moved from operator/scaler.py (Phase 2 refactoring).
"""

import logging
import math

logger = logging.getLogger("ppa.domain.scaling")


def calculate_replicas(
    predicted_load: float,
    current: int,
    min_replicas: int,
    max_replicas: int,
    capacity_per_pod: int,
    scale_up_rate: float,
    scale_down_rate: float,
    safety_factor: float = 1.10,
) -> int:
    """Calculate target replica count from LSTM forecast with rate limiting.

    Scaling Decision Logic:
    1. **Forecast-based:** target = ceil(forecast / capacity_per_pod)
    2. **Safety buffer:** forecast *= safety_factor (10% by default) before division
    3. **Rate limiting:** Prevent rapid oscillation:
       - Scale up: increase by at most (current * (scale_up_rate - 1))
       - Scale down: decrease by at most (current * (1 - scale_down_rate))
    4. **Bounds:** max(min_replicas, min(max_replicas, desired))

    Rate Limiting Design (PR#4):
    - Delta-based (not absolute floor) to enable proper convergence
    - Example: if current=10, scale_up_rate=1.5 → can add max 5 replicas
    - Example: if current=10, scale_down_rate=0.7 → can remove max 3 replicas
    - Prevents flip-flop oscillation while allowing gradual scaling down

    Safety Factor:
    - Applies 10% overbuild by default (1.1x multiplier)
    - Ensures capacity ready before demand arrives
    - Tunable per workload (aggressive apps: 1.2x, conservative: 1.05x)

    Args:
        predicted_load: LSTM forecast (RPS, connections, or other load metric)
        current: Current pod count (for delta calculations)
        min_replicas: Lower bound (typically 1-2)
        max_replicas: Upper bound (typically 50-100)
        capacity_per_pod: Load each pod can handle (RPS/pod, tuned per app)
        scale_up_rate: Multiplier limit on increases (e.g., 1.5 = max 50% per cycle)
        scale_down_rate: Multiplier limit on decreases (e.g., 0.7 = max 30% per cycle)
        safety_factor: Overbuild multiplier (default 1.1 = +10%, range 1.0-1.5)

    Returns:
        Desired replica count for kubectl/Kubernetes API

    Example:
        >>> # App can handle 50 RPS per pod, currently has 10 pods
        >>> forecast = 650  # LSTM predicts 650 RPS
        >>> desired = calculate_replicas(
        ...     predicted_load=650,
        ...     current=10,
        ...     min_replicas=1,
        ...     max_replicas=50,
        ...     capacity_per_pod=50,
        ...     scale_up_rate=1.5,      # Allow 50% increase per cycle
        ...     scale_down_rate=0.7,    # Allow 30% decrease per cycle
        ...     safety_factor=1.1,      # 10% headroom
        ... )
        >>> assert desired <= 10 + (10 * 0.5)  # Within rate limit
        >>> print(desired)  # ~15 (650 * 1.1 / 50 = 14.3, rounded)
        15

    Design Notes:
        - Idempotent: same inputs → same output
        - Gradual: prevents thundering herd (slow scaling up/down)
        - Safe: hard min/max enforced regardless of rate limiting
        - Observable: straightforward math (no hidden state)

    See Also:
        PR#4: Rate limiting design (prevents oscillation)
    """
    inflated = predicted_load * safety_factor
    raw = math.ceil(inflated / capacity_per_pod) if capacity_per_pod > 0 else current

    # FIX (PR#4): Delta-based rate limiting
    # Instead of using an absolute floor (current * scale_down_rate),
    # we apply limits based on how much we're allowed to change per step:
    # - Can increase by at most: current * (scale_up_rate - 1)
    # - Can decrease by at most: current * (1 - scale_down_rate)

    if raw >= current:
        # Scaling up: limit the increase
        max_increase = math.ceil(current * (scale_up_rate - 1))
        desired = min(current + max_increase, raw)
    else:
        # Scaling down: limit the decrease
        max_decrease = math.ceil(current * (1 - scale_down_rate))
        desired = max(current - max_decrease, raw)

    # Enforce hard bounds
    return max(min_replicas, min(max_replicas, desired))


def calculate_replicas_old(
    predicted_load: float,
    current: int,
    min_replicas: int,
    max_replicas: int,
    capacity_per_pod: int,
    scale_up_rate: float,
    scale_down_rate: float,
    safety_factor: float = 1.10,
) -> int:
    """Old implementation (buggy) - kept for testing comparison.

    DEPRECATED: Use calculate_replicas() instead. This is only for regression testing.
    """
    inflated = predicted_load * safety_factor
    raw = math.ceil(inflated / capacity_per_pod) if capacity_per_pod > 0 else current

    # Old rate limiting that prevented scale-down convergence
    max_up = max(1, math.ceil(current * scale_up_rate))
    min_down = max(1, math.floor(current * scale_down_rate))
    desired = max(min_down, min(max_up, raw))

    # Enforce hard bounds
    return max(min_replicas, min(max_replicas, desired))


def calculate_replicas_fixed(
    predicted_load: float,
    current: int,
    min_replicas: int,
    max_replicas: int,
    capacity_per_pod: int,
    scale_up_rate: float,
    scale_down_rate: float,
    safety_factor: float = 1.10,
) -> int:
    """Alias for the new fixed implementation (for testing backwards compatibility)."""
    return calculate_replicas(
        predicted_load,
        current,
        min_replicas,
        max_replicas,
        capacity_per_pod,
        scale_up_rate,
        scale_down_rate,
        safety_factor,
    )
