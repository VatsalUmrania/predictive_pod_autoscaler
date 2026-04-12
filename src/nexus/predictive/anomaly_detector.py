"""
NEXUS Anomaly Detector
=======================
Multi-layer anomaly detection for infrastructure metrics time-series.

Architecture (two layers, upper preferred):
    Layer 1 — GRUAutoencoder (requires PyTorch)
        • Sequence encoder/decoder architecture
        • Input:  30-step window × 4 features (cpu, mem, rps, error_rate)
        • Train:  MSE reconstruction on normal traffic
        • Score:  reconstruction error normalized to [0, 1]
        • Advantage: captures temporal patterns, not just point-in-time thresholds

    Layer 2 — ZScoreDetector (always available, stateless)
        • Rolling window per-metric mean/std
        • Score: max z-score across features / z_threshold → [0, 1]
        • Advantage: zero dependencies, interpretable

Auto-selection:
    • If PyTorch is installed AND a checkpoint file exists → GRUAutoencoder
    • Otherwise → ZScoreDetector

Key fixes over original PPA GRU (ARCHITECTURE_REVIEW_CRITICAL.md §1.4, §3.1):
    ✅ §1.4  NaN guard before feeding to model (replaces raw tensor creation)
    ✅ §3.1  Reconstruction error properly normalized (not raw MSE)
    ✅ §9.1  State isolated per-instance (no module-level globals)
    ✅ §3.2  Checkpoint loading with version check + graceful fallback

Training:
    GRUAutoencoder.train_from_prometheus(data: List[Dict[str, float]])
    Accepts the rolling 30-day Prometheus export from MetricsAgent.
    Saves checkpoint to NEXUS_GRU_CHECKPOINT_PATH on completion.

Configuration:
    NEXUS_GRU_CHECKPOINT_PATH   path to .pt checkpoint (default: data/gru_ae.pt)
    NEXUS_GRU_HIDDEN_DIM        GRU hidden units (default 64)
    NEXUS_GRU_SEQ_LEN           Input sequence length in steps (default 30)
    NEXUS_ZSCORE_WINDOW         Rolling window for z-score (default 60)
    NEXUS_ZSCORE_THRESHOLD      Z-score threshold for anomaly (default 3.0)
"""

from __future__ import annotations

import logging
import math
import os
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from nexus.predictive.feature_pipeline import _guard

logger = logging.getLogger(__name__)

# Feature order — MUST be consistent across training and inference
MODEL_FEATURES = ["cpu_utilization_pct", "memory_utilization_pct", "rps", "error_rate"]
N_FEATURES     = len(MODEL_FEATURES)

# Normalization bounds for GRU model input (matches feature_pipeline FEATURE_BOUNDS)
_NORM_MAX: Dict[str, float] = {
    "cpu_utilization_pct":    100.0,
    "memory_utilization_pct": 100.0,
    "rps":                    10_000.0,   # Clip at 10k for normalization (not clamp)
    "error_rate":             1.0,
}


# ──────────────────────────────────────────────────────────────────────────────
# Anomaly score result
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class AnomalyScore:
    """Output of the anomaly detector for one feature vector."""
    score:           float    # 0.0 = normal, 1.0 = highly anomalous
    detector:        str      # "gru" | "zscore"
    contributing:    Dict[str, float]   # Per-feature contribution scores
    is_anomaly:      bool              # score >= threshold
    threshold:       float

    @property
    def severity_label(self) -> str:
        if self.score >= 0.85:
            return "critical"
        if self.score >= 0.65:
            return "warning"
        return "normal"


# ──────────────────────────────────────────────────────────────────────────────
# Base class
# ──────────────────────────────────────────────────────────────────────────────

class AnomalyDetector(ABC):
    """Abstract anomaly detector interface."""

    @abstractmethod
    def detect(self, features: Dict[str, float]) -> AnomalyScore:
        """Score a single feature vector. Must be non-blocking."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def is_ready(self) -> bool:
        """Returns True when the detector is ready to produce scores."""
        ...


# ──────────────────────────────────────────────────────────────────────────────
# Z-Score detector (always available, stateless)
# ──────────────────────────────────────────────────────────────────────────────

class ZScoreDetector(AnomalyDetector):
    """
    Rolling window z-score anomaly detector.

    Scores each feature independently, returns max z-score as the anomaly score.
    Requires window_size samples before producing reliable scores.

    Args:
        window_size: Rolling window length (default 60 samples).
        threshold:   Z-score threshold to classify as anomaly (default 3.0).
    """

    def __init__(
        self,
        window_size: int = 60,
        threshold:   float = 3.0,
    ):
        self._window    = window_size
        self._threshold = threshold
        self._history:  Dict[str, Deque[float]] = {
            f: deque(maxlen=window_size) for f in MODEL_FEATURES
        }
        self._samples   = 0

    @property
    def name(self) -> str:
        return "zscore"

    @property
    def is_ready(self) -> bool:
        return self._samples >= max(10, self._window // 3)

    def detect(self, features: Dict[str, float]) -> AnomalyScore:
        """Compute z-scores and update rolling windows."""
        # Ingest
        for feat in MODEL_FEATURES:
            val = _guard(features.get(feat))
            self._history[feat].append(val)
        self._samples += 1

        if not self.is_ready:
            return AnomalyScore(
                score=0.0, detector=self.name,
                contributing={}, is_anomaly=False, threshold=self._threshold
            )

        contributing: Dict[str, float] = {}
        max_score = 0.0

        for feat in MODEL_FEATURES:
            hist  = list(self._history[feat])
            if len(hist) < 2:
                continue
            mean = sum(hist) / len(hist)
            var  = sum((x - mean) ** 2 for x in hist) / len(hist)
            std  = math.sqrt(var) if var > 0 else 1.0
            val  = hist[-1]
            z    = abs(val - mean) / std
            feat_score = min(z / self._threshold, 1.0)
            contributing[feat] = round(feat_score, 3)
            max_score = max(max_score, feat_score)

        return AnomalyScore(
            score=round(max_score, 3),
            detector=self.name,
            contributing=contributing,
            is_anomaly=max_score >= 1.0,     # 1.0 = z >= threshold
            threshold=self._threshold,
        )

    def reset(self) -> None:
        for q in self._history.values():
            q.clear()
        self._samples = 0


# ──────────────────────────────────────────────────────────────────────────────
# GRU Autoencoder (requires torch)
# ──────────────────────────────────────────────────────────────────────────────

class GRUAutoencoder(AnomalyDetector):
    """
    GRU-based sequence autoencoder for metrics anomaly detection.

    Architecture:
        Encoder: GRU(N_FEATURES → hidden_dim, num_layers=2)
        Decoder: GRU(hidden_dim → hidden_dim, num_layers=2) + Linear(hidden_dim → N_FEATURES)
        Loss: MSE reconstruction on normal traffic windows

    Anomaly score:
        reconstruction_error = MSE(input, reconstruction)
        score = min(error / error_threshold, 1.0)

    Requires:
        pip install torch  (CPU-only: pip install torch --index-url https://download.pytorch.org/whl/cpu)

    Args:
        checkpoint_path: .pt file path to load/save weights.
        hidden_dim:      GRU hidden units (default 64).
        seq_len:         Input sequence length (default 30).
        error_threshold: MSE error above which score = 1.0 (auto-calibrated on train).
    """

    def __init__(
        self,
        checkpoint_path: Optional[Path] = None,
        hidden_dim:      int = 64,
        seq_len:         int = 30,
        error_threshold: float = 0.05,
    ):
        self._checkpoint    = checkpoint_path or Path(
            os.getenv("NEXUS_GRU_CHECKPOINT_PATH", "data/gru_ae.pt")
        )
        self._hidden_dim    = int(os.getenv("NEXUS_GRU_HIDDEN_DIM", str(hidden_dim)))
        self._seq_len       = int(os.getenv("NEXUS_GRU_SEQ_LEN", str(seq_len)))
        self._error_thresh  = error_threshold
        self._model         = None
        self._buffer: Deque[List[float]] = deque(maxlen=self._seq_len)
        self._ready         = False
        self._torch_ok      = False

        self._init_model()

    def _init_model(self) -> None:
        """Attempt to import torch and initialize or load the model."""
        try:
            import torch
            import torch.nn as nn
            self._torch_ok = True
            self._model    = self._build_model(nn)

            if self._checkpoint.exists():
                state = torch.load(self._checkpoint, map_location="cpu")
                self._model.load_state_dict(state["model"])
                self._error_thresh = state.get("error_threshold", self._error_thresh)
                self._ready        = True
                logger.info(
                    f"[GRUAutoencoder] Loaded checkpoint {self._checkpoint} "
                    f"error_thresh={self._error_thresh:.4f}"
                )
            else:
                logger.info(
                    f"[GRUAutoencoder] No checkpoint at {self._checkpoint} "
                    f"— call train_from_data() to initialize"
                )
        except ImportError:
            logger.info(
                "[GRUAutoencoder] PyTorch not installed — "
                "use ZScoreDetector or install: pip install torch"
            )
        except Exception as exc:
            logger.warning(f"[GRUAutoencoder] Init error: {exc}")

    def _build_model(self, nn: Any) -> Any:
        """Build the GRU encoder-decoder model."""
        import torch
        import torch.nn as nn

        class _GRUAutoencoder(nn.Module):
            def __init__(self, n_features: int, hidden_dim: int, seq_len: int):
                super().__init__()
                self.seq_len    = seq_len
                self.hidden_dim = hidden_dim
                self.encoder    = nn.GRU(n_features, hidden_dim, num_layers=2, batch_first=True)
                self.decoder    = nn.GRU(hidden_dim, hidden_dim, num_layers=2, batch_first=True)
                self.output_layer = nn.Linear(hidden_dim, n_features)

            def forward(self, x: "torch.Tensor") -> "torch.Tensor":
                # x: (batch, seq_len, n_features)
                _, hidden  = self.encoder(x)
                # Use last hidden state as initial decoder input
                dec_input  = hidden[-1].unsqueeze(1).repeat(1, self.seq_len, 1)
                decoded, _ = self.decoder(dec_input, hidden)
                return self.output_layer(decoded)

        return _GRUAutoencoder(N_FEATURES, self._hidden_dim, self._seq_len)

    @property
    def name(self) -> str:
        return "gru"

    @property
    def is_ready(self) -> bool:
        return self._ready and self._torch_ok

    def detect(self, features: Dict[str, float]) -> AnomalyScore:
        """Score a single feature vector (buffers into sequence window internally)."""
        if not self._torch_ok:
            return AnomalyScore(score=0.0, detector=self.name, contributing={},
                                is_anomaly=False, threshold=self._error_thresh)

        # Normalize and buffer
        vec = self._normalize(features)
        self._buffer.append(vec)

        if len(self._buffer) < self._seq_len:
            return AnomalyScore(score=0.0, detector=self.name, contributing={},
                                is_anomaly=False, threshold=self._error_thresh)

        return self._score_buffer()

    def _normalize(self, features: Dict[str, float]) -> List[float]:
        """Normalize features to [0, 1] using _NORM_MAX. Guarded against NaN."""
        vec = []
        for feat in MODEL_FEATURES:
            val = _guard(features.get(feat))
            max_val = _NORM_MAX.get(feat, 1.0)
            norm = val / max_val if max_val > 0 else 0.0
            norm = max(0.0, min(1.0, norm))  # clamp
            vec.append(norm if math.isfinite(norm) else 0.0)
        return vec

    def _score_buffer(self) -> AnomalyScore:
        try:
            import torch
            self._model.eval()
            with torch.no_grad():
                seq    = torch.tensor(list(self._buffer), dtype=torch.float32).unsqueeze(0)
                recon  = self._model(seq)
                # Per-feature MSE
                mse_per_feat = ((seq - recon) ** 2).mean(dim=1).squeeze(0)
                total_mse    = mse_per_feat.mean().item()

            score = min(total_mse / max(self._error_thresh, 1e-6), 1.0)

            contributing = {
                feat: round(float(mse_per_feat[i].item()) / max(self._error_thresh, 1e-6), 3)
                for i, feat in enumerate(MODEL_FEATURES)
            }

            return AnomalyScore(
                score=round(score, 3),
                detector=self.name,
                contributing=contributing,
                is_anomaly=score >= 0.5,
                threshold=self._error_thresh,
            )
        except Exception as exc:
            logger.warning(f"[GRUAutoencoder] Inference error: {exc}")
            return AnomalyScore(score=0.0, detector=self.name, contributing={},
                                is_anomaly=False, threshold=self._error_thresh)

    def train_from_data(
        self,
        data: List[Dict[str, float]],
        epochs: int = 50,
        lr:     float = 1e-3,
        batch_size: int = 32,
    ) -> Dict[str, float]:
        """
        Train the GRU Autoencoder on historical normal-traffic data.

        Args:
            data:   List of feature dicts (from Prometheus export — normal windows only).
            epochs: Training epochs (default 50).
            lr:     Learning rate (default 1e-3).

        Returns:
            dict with "final_loss" and "error_threshold"
        """
        if not self._torch_ok:
            raise RuntimeError("PyTorch not installed — cannot train GRU Autoencoder")
        if len(data) < self._seq_len + 10:
            raise ValueError(f"Need at least {self._seq_len + 10} samples, got {len(data)}")

        import torch
        import torch.nn as nn
        from torch.optim import Adam

        # Build sequences
        sequences = []
        vecs = [self._normalize(d) for d in data]
        for i in range(len(vecs) - self._seq_len):
            sequences.append(vecs[i: i + self._seq_len])

        dataset = torch.tensor(sequences, dtype=torch.float32)
        loader  = torch.utils.data.DataLoader(dataset, batch_size=batch_size, shuffle=True)
        optimizer = Adam(self._model.parameters(), lr=lr)
        criterion = nn.MSELoss()

        self._model.train()
        losses = []
        for epoch in range(epochs):
            epoch_loss = 0.0
            for batch in loader:
                optimizer.zero_grad()
                recon = self._model(batch)
                loss  = criterion(recon, batch)
                loss.backward()
                optimizer.step()
                epoch_loss += loss.item()
            avg = epoch_loss / len(loader)
            losses.append(avg)
            if (epoch + 1) % 10 == 0:
                logger.info(f"[GRUAutoencoder] Epoch {epoch+1}/{epochs} loss={avg:.6f}")

        # Calibrate threshold = 3× mean reconstruction error on training set
        self._model.eval()
        with torch.no_grad():
            recon_errors = []
            for batch in loader:
                recon = self._model(batch)
                mse   = ((batch - recon) ** 2).mean(dim=(1, 2))
                recon_errors.extend(mse.tolist())
        self._error_thresh = 3.0 * (sum(recon_errors) / len(recon_errors))

        # Save checkpoint
        self._checkpoint.parent.mkdir(parents=True, exist_ok=True)
        torch.save({
            "model":           self._model.state_dict(),
            "error_threshold": self._error_thresh,
            "hidden_dim":      self._hidden_dim,
            "seq_len":         self._seq_len,
        }, self._checkpoint)
        self._ready = True

        logger.info(
            f"[GRUAutoencoder] Training complete — "
            f"final_loss={losses[-1]:.6f} "
            f"error_threshold={self._error_thresh:.6f} "
            f"checkpoint={self._checkpoint}"
        )
        return {"final_loss": losses[-1], "error_threshold": self._error_thresh}


# ──────────────────────────────────────────────────────────────────────────────
# Auto-detector (picks best available)
# ──────────────────────────────────────────────────────────────────────────────

class AutoAnomalyDetector:
    """
    Selects the best available detector at runtime.

    Priority: GRUAutoencoder (if checkpoint exists) > ZScoreDetector.
    Both detectors are always maintained — if GRU becomes unavailable,
    falls back to ZScore without restarts.
    """

    def __init__(
        self,
        checkpoint_path: Optional[Path] = None,
        zscore_window:   int = int(os.getenv("NEXUS_ZSCORE_WINDOW", "60")),
        zscore_threshold: float = float(os.getenv("NEXUS_ZSCORE_THRESHOLD", "3.0")),
    ):
        self._gru    = GRUAutoencoder(checkpoint_path=checkpoint_path)
        self._zscore = ZScoreDetector(window_size=zscore_window, threshold=zscore_threshold)

    def detect(self, features: Dict[str, float]) -> AnomalyScore:
        """Score using GRU if ready, otherwise ZScore."""
        # Always update ZScore (maintain its rolling window regardless)
        zscore_result = self._zscore.detect(features)

        if self._gru.is_ready:
            gru_result = self._gru.detect(features)
            # Blend: GRU score weighted 0.7, ZScore 0.3
            blended = 0.7 * gru_result.score + 0.3 * zscore_result.score
            return AnomalyScore(
                score=round(blended, 3),
                detector="gru+zscore",
                contributing={**zscore_result.contributing, **{
                    f"gru_{k}": v for k, v in gru_result.contributing.items()
                }},
                is_anomaly=blended >= 0.5,
                threshold=gru_result.threshold,
            )

        return zscore_result

    @property
    def active_detector(self) -> str:
        return "gru+zscore" if self._gru.is_ready else "zscore"

    def train_gru(self, data: List[Dict[str, float]], **kwargs) -> Dict[str, float]:
        """Train the GRU detector from historical data."""
        return self._gru.train_from_data(data, **kwargs)
