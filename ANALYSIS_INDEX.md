# PPA Model System - Complete Analysis Index

This directory contains comprehensive documentation of the Predictive Pod Autoscaler's model upload/promotion system, generated from a full codebase exploration.

## Analysis Documents

### 1. **EXPLORATION_SUMMARY.txt** (Quick Overview)
**Best for:** Getting a high-level understanding quickly
- Executive summary of findings
- 5 key findings with root causes
- Critical gaps identified (5 total)
- Immediate actions required
- Special features documented (PRs #5, #7, #8, #10, #12, #15)

**Read this first** to understand the problem and its scope.

### 2. **QUICK_REFERENCE.md** (Debugging & Operations)
**Best for:** Operational troubleshooting and day-to-day work
- Quick problem/solution reference
- Model path structure (3 stages)
- Model loading sequence breakdown
- Quick fixes (in priority order)
- Debugging checklist
- Testing procedures
- Backoff strategy explained
- Metadata validation explained

**Use this when:** Debugging model loading issues or running model promotion workflows.

### 3. **MODEL_SYSTEM_ANALYSIS.md** (Deep Dive - Comprehensive)
**Best for:** Understanding architecture and planning fixes
- 8 detailed sections covering all aspects
- Complete pipeline flow diagrams (ASCII art)
- 3 storage locations explained
- Root cause analysis with examples
- Error flow visualization
- Current deployment workflow
- 6 critical gaps identified with severity levels
- 5 recommended fixes with code examples
- Summary table of all issues
- Complete file reference guide

**Use this when:** Planning fixes, doing code review, or learning the system deeply.

## The Problem in 30 Seconds

```
Error: RuntimeError: No TFLite runtime found 
       (tried ai_edge_litert, tensorflow.lite, tflite_runtime)

Root Cause: requirements.operator.txt only lists ai-edge-litert==2.1.3
            If that binary isn't available, operator dies

Fix: Add tflite-runtime>=2.14.0 to requirements.operator.txt
```

## Quick Navigation

### I want to...

**Understand the model system architecture**
→ Read: MODEL_SYSTEM_ANALYSIS.md (Section 1-4)

**Fix the "No TFLite runtime found" error**
→ Read: QUICK_REFERENCE.md (Quick Fixes section) or EXPLORATION_SUMMARY.txt

**Debug a model loading failure**
→ Read: QUICK_REFERENCE.md (Debugging Checklist + Testing Model Load)

**See what files do what**
→ Read: Any document's "Files to Know" / "Key Files Identified" section

**Understand model promotion workflow**
→ Read: MODEL_SYSTEM_ANALYSIS.md (Section 4) or QUICK_REFERENCE.md (Model Promotion Workflow)

**Plan a fix PR**
→ Read: EXPLORATION_SUMMARY.txt (Immediate Actions) or MODEL_SYSTEM_ANALYSIS.md (Section 6)

**Understand why certain choices were made**
→ Read: QUICK_REFERENCE.md (Architecture Decisions section)

## Key Takeaways

### Model Flow (3 Stages)
1. **Development** → `data/artifacts/{app}/{namespace}/{horizon}/`
2. **Promotion** → `data/champions/{app}/{namespace}/{horizon}/`
3. **Runtime** → `/models/{app}/{horizon}/` (on Kubernetes PVC)

### Critical Path Points
- **Training**: `ppa model train` → Keras model
- **Conversion**: `ppa model convert` → TFLite model
- **Upload**: `ppa model push` → Models copied to K8s PVC
- **Loading**: Operator reads from PVC via `Predictor` class
- **Failure Point**: `src/ppa/operator/predictor.py:166-169` ← TFLite runtime loading

### The Root Cause
| Component | Issue | Impact |
|-----------|-------|--------|
| `requirements.operator.txt` | Only `ai-edge-litert==2.1.3` | If binary unavailable → operator dies |
| `src/ppa/operator/Dockerfile` | Minimal dependencies | No fallback TFLite runtime |
| `predictor.py:_try_load()` | Hard failure on missing runtime | No graceful degradation |

### Immediate Fixes (Priority Order)
1. Add `tflite-runtime>=2.14.0` to `requirements.operator.txt`
2. Create `src/ppa/loader/Dockerfile` template
3. Improve error messaging in `predictor.py`
4. Add pre-upload model validation
5. Document path conventions

## File Structure Reference

### Core Model Loading Files
```
src/ppa/operator/
├─ predictor.py          ← Where "No TFLite runtime found" error originates
├─ main.py               ← Orchestrates CR → Predictor
├─ Dockerfile            ← Operator image (needs TFLite runtime)
└─ scaler.py             ← Scaling operations
```

### Model Training & Conversion
```
src/ppa/model/
├─ train.py              ← Keras LSTM training
├─ convert.py            ← Keras → TFLite conversion
├─ evaluate.py           ← Model evaluation
├─ pipeline.py           ← Full train→convert→promote pipeline
├─ deployment.py         ← CRD path updates
└─ artifacts.py          ← Path helpers
```

### Model Promotion & Upload
```
src/ppa/cli/commands/
├─ model.py              ← `ppa model` CLI commands
├─ push.py               ← `ppa model push` uploads to K8s
└─ deploy_stages.py      ← Deploy stage implementations

src/ppa/runtime/
└─ regenerate_scalers.py ← Fixes pickle compatibility inside pod
```

### Configuration
```
src/ppa/
├─ config.py             ← DEFAULT_MODEL_DIR = "/models"
└─ common/
   └─ feature_spec.py    ← FEATURE_COLUMNS definition

deploy/
├─ crd.yaml              ← CRD definition with modelPath spec
├─ operator-deployment.yaml  ← Operator deployment + PVC mount
└─ model-upload-pod.yaml    ← Legacy model upload pod
```

## Analysis Scope

### What Was Analyzed
- ✅ Model upload/promotion logic (`ppa model push` command)
- ✅ Model file storage and path conventions
- ✅ Model loading in operator (`Predictor` class)
- ✅ TFLite runtime dependency management
- ✅ Kubernetes deployment configuration
- ✅ CRD specification and model path references
- ✅ Related PRs and special features (PR#5, #7, #8, #10, #12, #15)

### What Was Found
- ✅ 3-stage model promotion pipeline documented
- ✅ Root cause of TFLite error identified
- ✅ 6 critical gaps identified
- ✅ 5 recommended fixes with code examples
- ✅ Complete architecture flowcharts
- ✅ Debugging procedures
- ✅ Testing procedures

### Not Covered (Out of Scope)
- Retraining controller logic (separate system)
- Prometheus metric collection
- Scaling algorithm implementation
- Data collection pipeline
- Testing/validation framework

## How to Use This Analysis

### For Debugging
1. Start with QUICK_REFERENCE.md
2. Run through the debugging checklist
3. Use the testing model load section if needed
4. Check operator logs for specific error messages

### For Implementation
1. Read EXPLORATION_SUMMARY.txt for overview
2. Review the critical gap you're fixing in MODEL_SYSTEM_ANALYSIS.md
3. Check "Recommended Fixes" section for implementation details
4. Review related files in "Key Files Identified"

### For Code Review
1. Use MODEL_SYSTEM_ANALYSIS.md as reference documentation
2. Check section 8 for file locations and responsibilities
3. Use workflow diagrams to understand data flow
4. Reference the architecture decisions section

### For Future Explorers
1. Start with EXPLORATION_SUMMARY.txt (5 min read)
2. Then QUICK_REFERENCE.md for practical knowledge (10 min)
3. Then MODEL_SYSTEM_ANALYSIS.md for deep understanding (30 min)
4. Use index.md (this file) to navigate between documents

## Key Learnings

1. **Model Paths are Conventional**: No registration system; just a naming convention
2. **Pickle Compatibility Matters**: Scalers must be regenerated in target pod environment
3. **History Preservation is Clever**: PR#5 solves the "coldstart after model upgrade" problem
4. **Metadata Validation is Critical**: PR#7/PR#8 catch train/serve mismatches early
5. **No Graceful Degradation**: Currently hard-fails on TFLite runtime missing
6. **Multiple Storage Stages**: Development → Promotion → Runtime stages separate concerns
7. **Backoff Strategy is Sophisticated**: PR#10 allows recovery by installing runtime later

## Next Steps

### Immediate (This Sprint)
- [ ] Add `tflite-runtime` to requirements
- [ ] Rebuild operator image
- [ ] Test in your environment

### Short-term (Next Sprint)
- [ ] Create loader Dockerfile template
- [ ] Add model validation to push command
- [ ] Improve error messaging

### Medium-term (Planning)
- [ ] Document path conventions in CRD
- [ ] Add graceful degradation mode
- [ ] Add pre-flight runtime checks

## Questions?

Refer to the appropriate document:
- **Architecture**: MODEL_SYSTEM_ANALYSIS.md
- **Debugging**: QUICK_REFERENCE.md
- **Overview**: EXPLORATION_SUMMARY.txt

---

**Analysis Date**: April 10, 2026
**Repository**: predictive_pod_autoscaler
**Analysis Type**: Complete codebase exploration with architecture documentation
