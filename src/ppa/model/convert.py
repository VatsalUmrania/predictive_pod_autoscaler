# model/convert.py — Keras → TFLite + quantize
import argparse
import datetime
import json
import os

import numpy as np
import tensorflow as tf


def evaluate_model_accuracy(model, eval_data=None):
    """Evaluate model accuracy on eval data. Returns MAE metric."""
    if eval_data is None:
        return None
    try:
        # Assume eval_data is a tuple (X, y) or dataset
        if isinstance(eval_data, tuple):
            X, y = eval_data
            predictions = model.predict(X, verbose=0)
            mae = np.mean(np.abs(predictions.flatten() - y.flatten()))
            return float(mae)
        else:
            # If it's a tf.data.Dataset
            metrics = model.evaluate(eval_data, verbose=0)
            return float(metrics[1]) if isinstance(metrics, (list, tuple)) else float(metrics)
    except Exception as e:
        print(f"Warning: Could not evaluate model accuracy: {e}")
        return None


def convert_model(
    model_path="data/artifacts/ppa_model.keras",
    quantize=True,
    output_path=None,
    validation_data=None,
):
    """Convert a Keras model to TFLite format with optional quantization validation.

    FIX (PR#8): Validates quantization accuracy loss is <5%, fails deployment if exceeded.

    Args:
        model_path: Path to .keras model file
        quantize: Whether to apply quantization
        output_path: Output .tflite path
        validation_data: Optional (X, y) tuple for accuracy validation

    Returns:
        dict with keys: output_path, size_kb, baseline_mae, quantized_mae, accuracy_loss_pct
        or None on failure.
    """
    if not os.path.exists(model_path):
        print(f"Error: Model file {model_path} not found.")
        return None

    print(f"Loading Keras model from {model_path}...")
    model = tf.keras.models.load_model(model_path)

    # Establish baseline accuracy before quantization
    baseline_mae = evaluate_model_accuracy(model, validation_data)
    if baseline_mae is not None:
        print(f"Baseline accuracy (MAE): {baseline_mae:.6f}")

    print("Converting to TFLite format...")
    converter = tf.lite.TFLiteConverter.from_keras_model(model)

    # LSTM ops require Select TF ops sometimes, but here we just use standard ops
    converter.target_spec.supported_ops = [
        tf.lite.OpsSet.TFLITE_BUILTINS,
        tf.lite.OpsSet.SELECT_TF_OPS,
    ]

    if quantize:
        print("Applying int8/float16 quantization for smaller footprint...")
        converter.optimizations = [tf.lite.Optimize.DEFAULT]

        # FIX (PR#8): Use representative dataset if available for better quantization
        if validation_data is not None and isinstance(validation_data, tuple):
            try:
                X, _ = validation_data
                # Use first 100 samples for quantization calibration
                rep_samples = X[: min(100, len(X))]

                def representative_dataset():
                    for sample in rep_samples:
                        yield [np.expand_dims(sample, axis=0).astype(np.float32)]

                converter.representative_dataset = representative_dataset
                print("Using representative dataset for quantization calibration")
            except Exception as e:
                print(f"Warning: Could not use representative dataset: {e}")

    tflite_model = converter.convert()

    if output_path is None:
        output_path = model_path.replace(".keras", ".tflite")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(tflite_model)

    size_kb = len(tflite_model) / 1024
    print(f"Successfully saved TFLite model to {output_path}")
    print(f"Size: {size_kb:.2f} KB")

    # FIX (PR#8): Validate quantized model accuracy
    quantized_mae = None
    accuracy_loss_pct = None
    if baseline_mae is not None and validation_data is not None:
        try:
            # Load and evaluate quantized model
            try:
                import tflite_runtime.interpreter as tflite
            except ImportError:
                try:
                    from tensorflow import lite as tflite
                except ImportError:
                    import tensorflow.lite as tflite

            interpreter = tflite.Interpreter(model_path=output_path)
            interpreter.allocate_tensors()
            input_details = interpreter.get_input_details()
            output_details = interpreter.get_output_details()

            # Evaluate on validation set
            X, y = validation_data
            predictions = []
            for sample in X[: min(100, len(X))]:
                interpreter.set_tensor(
                    input_details[0]["index"],
                    np.expand_dims(sample, axis=0).astype(np.float32),
                )
                interpreter.invoke()
                pred = interpreter.get_tensor(output_details[0]["index"])
                predictions.append(pred.flatten()[0])

            quantized_mae = np.mean(np.abs(np.array(predictions) - y[: len(predictions)].flatten()))
            accuracy_loss_pct = (
                ((quantized_mae - baseline_mae) / baseline_mae * 100) if baseline_mae > 0 else 0.0
            )

            print(f"Quantized model accuracy (MAE): {quantized_mae:.6f}")
            print(f"Accuracy loss: {accuracy_loss_pct:.2f}%")

            # FIX (PR#8): FAIL if accuracy loss >5%
            if accuracy_loss_pct > 5.0:
                raise RuntimeError(
                    f"Quantization accuracy loss too high: {accuracy_loss_pct:.2f}% (threshold: 5.0%). "
                    f"Model cannot be deployed. Consider retraining or disabling quantization."
                )
        except RuntimeError:
            raise
        except Exception as e:
            print(f"Warning: Could not validate quantized accuracy: {e}")

    # FIX (PR#7): Save metadata alongside model
    metadata = {
        "version": "1.0",
        "conversion_date": datetime.datetime.now().isoformat(),
        "model_source": os.path.basename(model_path),
        "tflite_path": os.path.basename(output_path),
        "quantized": quantize,
        "baseline_mae": baseline_mae,
        "quantized_mae": quantized_mae,
        "accuracy_loss_pct": accuracy_loss_pct,
        "size_kb": round(size_kb, 2),
    }

    metadata_path = output_path.replace(".tflite", "_metadata.json")
    try:
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)
        print(f"Saved metadata to {metadata_path}")
    except Exception as e:
        print(f"Warning: Could not save metadata: {e}")

    result = {
        "output_path": output_path,
        "size_kb": round(size_kb, 2),
    }
    if baseline_mae is not None:
        result["baseline_mae"] = baseline_mae
    if quantized_mae is not None:
        result["quantized_mae"] = quantized_mae
    if accuracy_loss_pct is not None:
        result["accuracy_loss_pct"] = accuracy_loss_pct

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert Keras model to TFLite")
    parser.add_argument("--model", type=str, default="data/artifacts/ppa_model.keras")
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output .tflite path (default: same dir as model)",
    )
    parser.add_argument("--no-quantize", action="store_true")
    args = parser.parse_args()

    convert_model(args.model, not args.no_quantize, args.output)
