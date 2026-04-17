"""ML model training and evaluation.

Note: Full model functionality requires TensorFlow to be installed.
Use: pip install tensorflow
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ppa.model.convert import convert_model
    from ppa.model.deployment import patch_predictiveautoscaler_paths
    from ppa.model.evaluate import (
        compute_mae,
        compute_mape,
        compute_rmse,
        compute_scaling_stats,
        evaluate_model,
        rps_to_replicas,
    )
    from ppa.model.model_qualifier import load_json, should_promote
    from ppa.model.train import (
        LOOKBACK_STEPS,
        create_dataset_from_segments,
        train_model,
    )

# Runtime imports with fallbacks for missing TensorFlow
_TENSORFLOW_AVAILABLE = False

try:
    from ppa.model.convert import convert_model as convert_model
    from ppa.model.deployment import (
        patch_predictiveautoscaler_paths as patch_predictiveautoscaler_paths,
    )
    from ppa.model.model_qualifier import load_json as load_json
    from ppa.model.model_qualifier import should_promote as should_promote
    from ppa.model.train import (
        LOOKBACK_STEPS as LOOKBACK_STEPS,
    )
    from ppa.model.train import (
        create_dataset_from_segments as create_dataset_from_segments,
    )
    from ppa.model.train import (
        train_model as train_model,
    )

    _TENSORFLOW_AVAILABLE = True
except ImportError:
    # Fallback: stubs for type consistency at runtime
    convert_model = None  # type: ignore[assignment]
    patch_predictiveautoscaler_paths = None  # type: ignore[assignment]
    load_json = None  # type: ignore[assignment]
    should_promote = None  # type: ignore[assignment]
    LOOKBACK_STEPS = None  # type: ignore[assignment]
    create_dataset_from_segments = None  # type: ignore[assignment]
    train_model = None  # type: ignore[assignment]

try:
    from ppa.model.evaluate import (
        compute_mae as compute_mae,
    )
    from ppa.model.evaluate import (
        compute_mape as compute_mape,
    )
    from ppa.model.evaluate import (
        compute_rmse as compute_rmse,
    )
    from ppa.model.evaluate import (
        compute_scaling_stats as compute_scaling_stats,
    )
    from ppa.model.evaluate import (
        evaluate_model as evaluate_model,
    )
    from ppa.model.evaluate import (
        rps_to_replicas as rps_to_replicas,
    )
except ImportError:
    # Fallback: stubs for type consistency at runtime
    evaluate_model = None  # type: ignore[assignment]
    compute_mae = None  # type: ignore[assignment]
    compute_rmse = None  # type: ignore[assignment]
    compute_mape = None  # type: ignore[assignment]
    compute_scaling_stats = None  # type: ignore[assignment]
    rps_to_replicas = None  # type: ignore[assignment]

__all__ = [
    "train_model",
    "create_dataset_from_segments",
    "LOOKBACK_STEPS",
    "evaluate_model",
    "compute_mae",
    "compute_rmse",
    "compute_mape",
    "compute_scaling_stats",
    "rps_to_replicas",
    "convert_model",
    "should_promote",
    "load_json",
    "patch_predictiveautoscaler_paths",
]
