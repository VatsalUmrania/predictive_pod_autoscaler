# PPA Model System Analysis - START HERE

This directory contains **complete documentation** of the Predictive Pod Autoscaler's model upload/promotion system, with detailed analysis of the "No TFLite runtime found" error and how to fix it.

## The Problem in 30 Seconds

```
❌ ERROR: RuntimeError: No TFLite runtime found 
          (tried ai_edge_litert, tensorflow.lite, tflite_runtime)

🔍 ROOT CAUSE: requirements.operator.txt only has ai-edge-litert==2.1.3
               If that binary isn't available → operator dies

✅ FIX: Add tflite-runtime>=2.14.0 to requirements.operator.txt
```

## Documents (Pick Your Path)

### Path 1: I Just Need the Facts (2 min)
**Read:** `EXPLORATION_SUMMARY.txt`
- What's broken and why
- 5 critical gaps identified
- Immediate actions to take

### Path 2: I Need to Debug This (5 min)
**Read:** `QUICK_REFERENCE.md`
- Debugging checklist
- Quick fixes prioritized
- Model paths explained
- Testing procedures

### Path 3: I Want to Understand the Architecture (15 min)
**Read:** `MODEL_SYSTEM_ANALYSIS.md`
- Complete system design
- Flow diagrams
- Root cause analysis
- Detailed fixes with code examples

### Path 4: I'm Not Sure Where to Start (3 min)
**Read:** `ANALYSIS_INDEX.md`
- Navigation guide for all documents
- Quick reference to key takeaways
- File structure explanation

## The Model Flow (What Needs to Work)

```
STAGE 1: Train Locally
  └─ ppa model train
  └─ Output: ppa_model_{horizon}.keras
  └─ Location: data/artifacts/{app}/{namespace}/{horizon}/

STAGE 2: Convert & Promote Locally
  ├─ ppa model convert → ppa_model_{horizon}.tflite
  ├─ Copy to → data/champions/{app}/{namespace}/{horizon}/
  └─ Files: ppa_model.tflite, scaler.pkl, target_scaler.pkl

STAGE 3: Upload to Kubernetes
  ├─ ppa model push
  ├─ Creates loader pod
  ├─ Regenerates scalers
  └─ Result: /models/{app}/{horizon}/ on K8s PVC

STAGE 4: Operator Loads Model (THIS IS WHERE IT FAILS)
  ├─ Reads CRD spec for paths
  ├─ Creates Predictor class
  ├─ Attempts to load TFLite interpreter
  │  ├─ Try: ai_edge_litert ← Only option currently installed
  │  ├─ Try: tensorflow.lite ← Not installed
  │  └─ Try: tflite_runtime ← Not installed
  └─ ❌ All fail → RuntimeError raised
```

## Key Finding

The operator Dockerfile **only installs** what's in `requirements.operator.txt`:

| Package | Status | Notes |
|---------|--------|-------|
| ai-edge-litert==2.1.3 | ✅ Installed | Platform-specific, may fail |
| tensorflow | ❌ Missing | Too large (500MB) |
| tflite-runtime | ❌ Missing | Should be the fallback (~40MB) |

**The Fix:** Add `tflite-runtime>=2.14.0` to `requirements.operator.txt`

## Quick Fixes (In Priority Order)

### Fix 1: Add TFLite Runtime (5 min)
```bash
# Edit requirements.operator.txt
# Add this line:
tflite-runtime>=2.14.0

# Rebuild operator image:
docker build -f src/ppa/operator/Dockerfile -t ppa-operator:latest .
minikube image load ppa-operator:latest  # If using minikube
```

### Fix 2: Verify Model Files Exist
```bash
# Check PVC has models:
kubectl exec -it deployment/ppa-operator -- \
  ls -la /models/test-app/rps_t3m/
  
# Should see: ppa_model.tflite, scaler.pkl, target_scaler.pkl
```

### Fix 3: Test Model Loading
```bash
# SSH into operator and test:
kubectl exec -it deployment/ppa-operator -- bash

# Inside pod:
python3 << 'PYTHON'
from ppa.operator.predictor import Predictor
try:
    p = Predictor(
        "/models/test-app/rps_t3m/ppa_model.tflite",
        "/models/test-app/rps_t3m/scaler.pkl"
    )
    if p.ready():
        print("✓ Model loaded successfully!")
    else:
        print("✗ Model not ready (warming up)")
except Exception as e:
    print(f"✗ Error: {e}")
PYTHON
```

## Files Referenced in Analysis

### Core Model Loading (Where error originates)
- `src/ppa/operator/predictor.py:121-169` ← The _try_load() function
- `src/ppa/operator/main.py:113-128` ← Path resolution
- `src/ppa/operator/Dockerfile` ← Operator image build

### Model Promotion Pipeline
- `src/ppa/cli/commands/push.py` ← Upload models to K8s
- `src/ppa/model/pipeline.py` ← Train → Convert → Promote
- `src/ppa/runtime/regenerate_scalers.py` ← Fix pickle compatibility

### Configuration
- `src/ppa/config.py` ← DEFAULT_MODEL_DIR = "/models"
- `deploy/crd.yaml` ← CRD spec with model path fields
- `deploy/operator-deployment.yaml` ← PVC mounting config

## What Was Discovered

### 5 Critical Gaps
1. **Missing TFLite runtime** (CRITICAL) - No fallback when ai-edge-litert fails
2. **No graceful degradation** (HIGH) - Operator hard-fails instead of observer mode
3. **Missing loader Dockerfile** (MEDIUM) - Falls back to generic image
4. **No model validation** (LOW) - Errors not caught until runtime
5. **Unclear path conventions** (LOW) - Not documented in CRD

### 5 Recommended Fixes
1. Add tflite-runtime to requirements (IMMEDIATE)
2. Create loader Dockerfile template (This sprint)
3. Improve error messaging (This sprint)
4. Add pre-upload validation (Next sprint)
5. Document path conventions (Next sprint)

### Special Features Found
- ✅ PR#5: History preservation on model upgrade (smooth upgrades)
- ✅ PR#7/8: Metadata validation (catch schema mismatches)
- ✅ PR#10: Exponential backoff (retry on failure)
- ✅ PR#12: Concept drift detection (model degradation)
- ✅ PR#15: History serialization (pod restart resilience)

## Architecture Decisions

**Why three storage stages?**
- Development (artifacts) → Training workspace, might have multiple versions
- Promotion (champions) → Canonical models before deployment
- Runtime (/models on PVC) → What operator actually uses

**Why regenerate scalers inside pod?**
- Pickle compatibility across sklearn versions
- Development machine might have different sklearn than operator pod

**Why convention over configuration?**
- No registration system needed
- Path pattern: `/models/{app}/{horizon}/ppa_model.tflite`
- CRD can override if needed

## Next Steps

1. **This Week:** Add tflite-runtime, rebuild operator, test
2. **This Sprint:** Create loader Dockerfile, add validation
3. **Next Sprint:** Improve error handling, document paths

## Need More Detail?

| Need | Read This |
|------|-----------|
| Problem overview | EXPLORATION_SUMMARY.txt |
| How to debug | QUICK_REFERENCE.md |
| How it works | MODEL_SYSTEM_ANALYSIS.md |
| Which doc to read | ANALYSIS_INDEX.md |

---

**All documents are in the project root.**

Start with this file, then pick your path above. Ready? Let's go! 🚀
