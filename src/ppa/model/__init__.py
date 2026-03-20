"""ML model training and evaluation.

Note: Full model functionality requires TensorFlow to be installed.
Use: pip install tensorflow
"""

try:
    from ppa.model.convert import convert_model
    from ppa.model.train import (
        LOOKBACK_STEPS,
        create_dataset_from_segments,
        train_model,
    )

    _TENSORFLOW_AVAILABLE = True
except ImportError:
    train_model = None  # type: ignore[assignment]
    create_dataset_from_segments = None  # type: ignore[assignment]
    LOOKBACK_STEPS = None  # type: ignore[assignment]
    convert_model = None  # type: ignore[assignment]
    _TENSORFLOW_AVAILABLE = False

try:
    from ppa.model.evaluate import (
        compute_mae,
        compute_mape,
        compute_rmse,
        compute_scaling_stats,
        evaluate_model,
        rps_to_replicas,
    )
except ImportError:
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
]
