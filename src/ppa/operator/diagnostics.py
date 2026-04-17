"""Diagnostics module for runtime debugging of TFLite model loading issues."""

import logging
import sys
from pathlib import Path
from typing import Any, TypedDict, cast

logger = logging.getLogger("ppa.diagnostics")


class FileValidationResult(TypedDict, total=False):
    """Type for individual file validation results."""
    path: str
    exists: bool
    is_file: bool
    size_bytes: int | None
    readable: bool
    reason: str

class ModelFormatResult(TypedDict, total=False):
    """Type for model format validation results."""
    path: str
    valid_magic: bool
    reason: str | None

class DiagnosticsReport(TypedDict, total=False):
    """Type for comprehensive diagnostics report."""

    platform: dict[str, str]
    runtime: dict[str, str | None]
    files: dict[str, Any]  # Complex nested structure
    format: ModelFormatResult
    recommendations: list[str]

__all__ = [
    "check_tflite_runtime",
    "validate_model_files",
    "get_platform_info",
    "diagnose_model_load_issue",
]


def get_platform_info() -> dict[str, str]:
    """Get platform and Python runtime information."""
    import platform

    return {
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": platform.platform(),
        "architecture": platform.architecture()[0],
        "implementation": platform.python_implementation(),
    }


def check_tflite_runtime(include_tensorflow: bool = False) -> dict[str, str | None]:
    """
    Check which TFLite runtimes are available and working.

    Returns dict with keys: ai_edge_litert, tensorflow, tflite_runtime
    Values are version strings if available, None if not found, or error message if broken.
    """
    if include_tensorflow:
        try:
            import tensorflow as tf

            logger.info(f"tensorflow: {tf.__version__}")
        except ImportError:
            logger.debug("tensorflow not found (expected - adds 500MB)")
        except Exception as e:
            logger.warning(f"tensorflow import failed: {e}")

    results = {}

    # Check ai_edge_litert
    try:
        import ai_edge_litert

        results["ai_edge_litert"] = getattr(ai_edge_litert, "__version__", "available")
        logger.info(f"ai_edge_litert: {results['ai_edge_litert']}")
    except ImportError as e:
        results["ai_edge_litert"] = None
        logger.debug(f"ai_edge_litert not found: {e}")
    except Exception as e:
        results["ai_edge_litert"] = f"ERROR: {type(e).__name__}: {e}"
        logger.warning(f"ai_edge_litert import failed: {e}")

    # Check tensorflow.lite
    try:
        import tensorflow as tf

        results["tensorflow"] = tf.__version__
        logger.info(f"tensorflow: {results['tensorflow']}")
    except ImportError:
        results["tensorflow"] = None
        logger.debug("tensorflow not found (expected - adds 500MB)")
    except Exception as e:
        results["tensorflow"] = f"ERROR: {type(e).__name__}: {e}"
        logger.warning(f"tensorflow import failed: {e}")

    # Check tflite_runtime
    try:
        import tflite_runtime

        results["tflite_runtime"] = getattr(tflite_runtime, "__version__", "available")
        logger.info(f"tflite_runtime: {results['tflite_runtime']}")
    except ImportError as e:
        results["tflite_runtime"] = None
        logger.debug(f"tflite_runtime not found: {e}")
    except Exception as e:
        results["tflite_runtime"] = f"ERROR: {type(e).__name__}: {e}"
        logger.warning(f"tflite_runtime import failed: {e}")

    return results


def validate_model_files(
    model_path: str, scaler_path: str, target_scaler_path: str | None = None
) -> dict[str, Any]:
    """
    Validate model and scaler files exist and are accessible.

    Returns dict with validation results for each file.
    """
    results = {}

    for name, path in [
        ("model", model_path),
        ("scaler", scaler_path),
        ("target_scaler", target_scaler_path),
    ]:
        if path is None:
            results[name] = {"exists": False, "reason": "path is None"}
            continue

        p = Path(path)
        result = {
            "path": str(p),
            "exists": p.exists(),
            "is_file": p.is_file(),
            "size_bytes": p.stat().st_size if p.exists() else None,
            "readable": p.is_file() and p.stat().st_mode & 0o400 if p.exists() else False,
        }

        if not p.exists():
            result["reason"] = f"File not found: {p}"
            logger.warning(f"{name}: {result['reason']}")
        elif not result["is_file"]:
            result["reason"] = f"Path is not a file: {p}"
            logger.warning(f"{name}: {result['reason']}")
        elif not result["readable"]:
            result["reason"] = f"File not readable: {p}"
            logger.warning(f"{name}: {result['reason']}")
        else:
            result["reason"] = "OK"
            logger.info(f"{name}: {result['reason']} ({result['size_bytes']} bytes)")

        results[name] = result

    return results


def validate_model_format(model_path: str) -> dict[str, Any]:
    """
    Validate that the model file is a valid TFLite format.

    Returns dict with validation results.
    """
    p = Path(model_path)
    result = {
        "path": str(p),
        "valid_magic": False,
        "reason": None,
    }

    if not p.exists():
        result["reason"] = f"File not found: {p}"
        return result

    try:
        with open(p, "rb") as f:
            magic = f.read(4)
            # TFLite files start with "TFL3" (0x5443 0x4c46 in little-endian for "TFL" + version byte)
            if magic == b"TFL3":
                result["valid_magic"] = True
                result["reason"] = "Valid TFLite model format"
                logger.info("Model format: TFLite (valid)")
            else:
                result["reason"] = f"Invalid magic bytes: {magic!r} (expected b'TFL3')"
                logger.warning(f"Model format: {result['reason']}")
    except Exception as e:
        result["reason"] = f"Error reading file: {type(e).__name__}: {e}"
        logger.warning(f"Model validation error: {result['reason']}")

    return result


def diagnose_model_load_issue(
    model_path: str,
    scaler_path: str,
    target_scaler_path: str | None = None,
) -> DiagnosticsReport:
    """
    Comprehensive diagnostic report for model loading issues.

    Returns dict with sections: platform, runtime, files, format, recommendations.
    """
    logger.info("Starting comprehensive model load diagnostics...")

    platform_info = get_platform_info()
    runtime_info = check_tflite_runtime(include_tensorflow=False)
    files_info = validate_model_files(model_path, scaler_path, target_scaler_path)
    format_info: ModelFormatResult = cast(ModelFormatResult, validate_model_format(model_path))

    report: DiagnosticsReport = {
        "platform": platform_info,
        "runtime": runtime_info,
        "files": files_info,
        "format": format_info,
        "recommendations": [],
    }

    # Generate recommendations based on findings
    # Check if any runtime is available
    available_runtimes = [
        k for k, v in runtime_info.items() if v is not None and not v.startswith("ERROR")
    ]
    if not available_runtimes:
        report["recommendations"].append(
            "CRITICAL: No TFLite runtime found. Install tflite-runtime or tensorflow."
        )
    elif len(available_runtimes) == 1:
        report["recommendations"].append(
            f"WARNING: Only {available_runtimes[0]} available. Consider adding fallback runtime."
        )

    # Check file issues
    if not files_info["model"].get("exists"):
        report["recommendations"].append(
            f"CRITICAL: Model file not found at {files_info['model'].get('path')}"
        )
    elif not files_info["model"].get("valid_magic"):
        report["recommendations"].append(
            "ERROR: Model file is not valid TFLite format. File may be corrupted."
        )

    if not files_info["scaler"].get("exists"):
        report["recommendations"].append(
            f"CRITICAL: Scaler file not found at {files_info['scaler'].get('path')}"
        )

    if target_scaler_path and not files_info["target_scaler"].get("exists"):
        report["recommendations"].append(
            f"WARNING: Target scaler file not found at {files_info['target_scaler'].get('path')} "
            f"(optional, but recommended)"
        )

    # Check for broken runtime implementations
    for runtime_name, status in runtime_info.items():
        if isinstance(status, str) and status.startswith("ERROR"):
            report["recommendations"].append(f"ERROR: {runtime_name} import failed: {status}")

    if not report["recommendations"]:
        report["recommendations"].append("All checks passed. Model should be loadable.")

    return report


def print_diagnostics(report: DiagnosticsReport) -> None:
    """Pretty-print diagnostics report to logger."""
    logger.info("=" * 70)
    logger.info("MODEL LOAD DIAGNOSTICS REPORT")
    logger.info("=" * 70)

    logger.info("\n[PLATFORM]")
    for key, value in report["platform"].items():
        logger.info(f"  {key}: {value}")

    logger.info("\n[TFLITE RUNTIMES]")
    for runtime, status in report["runtime"].items():
        if status is None:
            logger.info(f"  {runtime}: NOT INSTALLED")
        elif isinstance(status, str) and status.startswith("ERROR"):
            logger.error(f"  {runtime}: {status}")
        else:
            logger.info(f"  {runtime}: {status}")

    logger.info("\n[FILES]")
    for name, info in report["files"].items():
        logger.info(f"  {name}:")
        logger.info(f"    path: {info.get('path', 'N/A')}")
        logger.info(f"    exists: {info.get('exists', False)}")
        if info.get("exists"):
            logger.info(f"    size: {info.get('size_bytes', 'N/A')} bytes")
        logger.info(f"    status: {info.get('reason', 'Unknown')}")

    logger.info("\n[FORMAT]")
    logger.info(f"  valid_magic: {report['format'].get('valid_magic', False)}")
    logger.info(f"  status: {report['format'].get('reason', 'Unknown')}")

    logger.info("\n[RECOMMENDATIONS]")
    for i, rec in enumerate(report["recommendations"], 1):
        logger.info(f"  {i}. {rec}")

    logger.info("=" * 70)
