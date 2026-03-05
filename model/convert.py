# model/convert.py — Keras → TFLite + quantize
import os
import argparse
import tensorflow as tf

def convert_model(model_path="model/artifacts/ppa_model.keras", quantize=True):
    if not os.path.exists(model_path):
        print(f"Error: Model file {model_path} not found.")
        return
        
    print(f"Loading Keras model from {model_path}...")
    model = tf.keras.models.load_model(model_path)
    
    print("Converting to TFLite format...")
    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    
    # LSTM ops require Select TF ops sometimes, but here we just use standard ops
    converter.target_spec.supported_ops = [
        tf.lite.OpsSet.TFLITE_BUILTINS,
        tf.lite.OpsSet.SELECT_TF_OPS
    ]
    
    if quantize:
        print("Applying int8/float16 quantization for smaller footprint...")
        converter.optimizations = [tf.lite.Optimize.DEFAULT]
        
    tflite_model = converter.convert()
    
    output_path = model_path.replace(".keras", ".tflite")
    with open(output_path, "wb") as f:
        f.write(tflite_model)
        
    print(f"Successfully saved TFLite model to {output_path}")
    print(f"Size: {len(tflite_model) / 1024:.2f} KB")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=str, default="model/artifacts/ppa_model.keras")
    parser.add_argument("--no-quantize", action="store_true")
    args = parser.parse_args()
    
    convert_model(args.model, not args.no_quantize)
