import pandas as pd
import sys
import os

def validate(csv_path):
    print(f"\nrunning validation on {csv_path}...\n")
    if not os.path.exists(csv_path):
        print(f"File not found: {csv_path}")
        return False
        
    df = pd.read_csv(csv_path, index_col=0)
    print(f"Loaded {len(df)} rows.")
    
    errors = []
    warnings = []
    
    # Min rows threshold (Warning if small, error if extremely small)
    if len(df) < 50:
        errors.append(f"Insufficient rows: {len(df)}, need at least 50 for testing.")
    elif len(df) < 10000:
        warnings.append(f"Low row count: {len(df)}, target is 10,000 for training.")
        
    # NaN ratio per column
    nan_ratio = df.isna().sum() / len(df)
    for col, ratio in nan_ratio.items():
        if ratio > 0.05:
            errors.append(f"High NaN ratio in {col}: {ratio:.2%}")
            
    # Zero/near-zero variance columns
    for col in df.columns:
        if df[col].std() < 1e-4:
            errors.append(f"Zero or near-zero variance in {col}.")
            
    # Duplicate/near-duplicate columns (correlation > 0.98)
    corr = df.corr().abs()
    for i in range(len(corr.columns)):
        for j in range(i+1, len(corr.columns)):
            col1, col2 = corr.columns[i], corr.columns[j]
            # Ignore target vs replica relationships and predictable temporal correlations
            if ("rps_t" in col1 and "replicas_t" in col2) or ("rps_t" in col2 and "replicas_t" in col1):
                continue
            if "sin" in col1 or "cos" in col1 or "sin" in col2 or "cos" in col2:
                continue
            
            cor_val = getattr(corr.iloc[i, j], "item", lambda: corr.iloc[i, j])()
            if cor_val > 0.98:
                errors.append(f"High correlation (>0.98) between {col1} and {col2}: {cor_val:.3f}")
                
    # Target availability and basic distribution
    targets = ["rps_t5", "rps_t10", "rps_t15"]
    for t in targets:
        if t not in df.columns:
            errors.append(f"Target column missing: {t}")
            
    if warnings:
        for w in warnings:
            print(f"  ⚠️  {w}")

    if errors:
        print("\n❌ Validation failed:")
        for err in errors:
            print(f"  - {err}")
        return False
        
    print("\n✅ Validation passed!")
    return True

if __name__ == "__main__":
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "data-collection/training-data/training_data.csv"
    if not validate(csv_path):
        sys.exit(1)
