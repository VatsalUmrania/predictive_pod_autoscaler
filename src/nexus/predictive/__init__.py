# nexus.predictive — Phase 5 Predictive Layer
# =============================================
# DB query pattern → traffic spike prediction → pre-scale recommendation

from nexus.predictive.feature_pipeline       import FeaturePipeline, QuerySnapshot, FeatureVector
from nexus.predictive.db_traffic_correlator  import DBTrafficCorrelator, TableEndpointMapper
from nexus.predictive.anomaly_detector       import (
    AnomalyScore,
    AnomalyDetector,
    ZScoreDetector,
    GRUAutoencoder,
    AutoAnomalyDetector,
    MODEL_FEATURES,
)
from nexus.predictive.traffic_model          import EWMATrafficModel, TrafficPrediction, SMAPETracker
from nexus.predictive.prescaler              import (
    Prescaler,
    PrescaleMode,
    PrescaleDecision,
    PrecisionStats,
    PrecisionTracker,
)

__all__ = [
    # Feature pipeline
    "FeaturePipeline",
    "QuerySnapshot",
    "FeatureVector",
    # DB traffic correlator
    "DBTrafficCorrelator",
    "TableEndpointMapper",
    # Anomaly detection
    "AnomalyScore",
    "AnomalyDetector",
    "ZScoreDetector",
    "GRUAutoencoder",
    "AutoAnomalyDetector",
    "MODEL_FEATURES",
    # Traffic model
    "EWMATrafficModel",
    "TrafficPrediction",
    "SMAPETracker",
    # Prescaler
    "Prescaler",
    "PrescaleMode",
    "PrescaleDecision",
    "PrecisionStats",
    "PrecisionTracker",
]
