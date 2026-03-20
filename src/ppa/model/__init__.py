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
    train_model = None
    create_dataset_from_segments = None
    LOOKBACK_STEPS = None
    convert_model = None
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
    evaluate_model = None
    compute_mae = None
    compute_rmse = None
    compute_mape = None
    compute_scaling_stats = None
    rps_to_replicas = None

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
