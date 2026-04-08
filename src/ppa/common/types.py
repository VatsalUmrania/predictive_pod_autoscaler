"""Shared type definitions used across PPA modules.

This module centralizes types to avoid circular dependencies.
For example, Predictor type is imported here instead of from ppa.operator.predictor
to prevent domain module from depending on operator module.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ppa.operator.predictor import Predictor

# Make Predictor available as a type hint
__all__ = ["Predictor"]
