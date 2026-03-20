#!/bin/bash
# Live HPA vs PPA Comparison with t+10 Prediction Validation
# Fully working version - FIXED field extraction

PROMETHEUS_URL="http://localhost:9090"
PREDICTION_LOG="prediction_validation.log"
PREDICTIONS_FILE="/tmp/ppa_predictions.txt"

# Initialize log if needed
if [[ ! -f "$PREDICTION_LOG" ]]; then
  echo "timestamp,predicted_rps,actual_rps_10min_later,error_percent,accuracy" > "$PREDICTION_LOG"
fi

touch "$PREDICTIONS_FILE"

# ── Helper Functions ──────────────────────────────────────────────────
log_msg() { echo -e "${1}"; }
info_msg() { echo -e "\033[0;36m${1}\033[0m"; }
success_msg() { echo -e "\033[0;32m${1}\033[0m"; }
warn_msg() { echo -e "\033[1;33m${1}\033[0m"; }
error_msg() { echo -e "\033[0;31m${1}\033[0m"; }

# Query Prometheus with error handling
query_prometheus() {
  local query="$1"
  local result=$(curl -sg "${PROMETHEUS_URL}/api/v1/query?query=$(echo ${query} | sed 's/ /%20/g')" 2>/dev/null | \
    python3 -c "
import json,sys
try:
    d = json.load(sys.stdin)
    if d.get('data', {}).get('result'):
        val = d['data']['result'][0]['value'][1]
        print(round(float(val), 2))
    else:
        print('N/A')
except:
    print('N/A')
" 2>/dev/null)
  echo "$result"
}

# Get PPA status from CR (CORRECT FIELDS)
get_ppa_status() {
  kubectl get ppa test-app-ppa -o jsonpath='{.status}' 2>/dev/null | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    print(f\"{data.get('desiredReplicas', '?')}|{data.get('currentReplicas', '?')}|{data.get('lastPredictedLoad', '?')}\")
except:
    print('?|?|?')
" 2>/dev/null || echo "?|?|?"
}

# Extract predicted RPS from operator logs
get_ppa_predicted_rps_from_logs() {
  kubectl logs deployment/ppa-operator --tail=30 2>/dev/null | \
    grep "Predicted load:" | \
    grep -oP "Predicted load:\s*\K[0-9.]+" | tail -1 || echo "?"
}

# ══════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════

while true; do
  clear
  TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
  TIMESTAMP_EPOCH=$(date +%s)
  TIMESTAMP_READABLE=$(date '+%H:%M:%S')
  
  log_msg "╔════════════════════════════════════════════════════════════╗"
  log_msg "║  HPA vs PPA — Live t+10 Prediction Validation             ║"
  log_msg "║  ${TIMESTAMP}                              ║"
  log_msg "╚════════════════════════════════════════════════════════════╝"
  
  # ────────────────────────────────────────────────────────────────────
  # SECTION 1: CURRENT STATE
  # ────────────────────────────────────────────────────────────────────
  info_msg "\n📊 CURRENT SCALING STATE"
  log_msg "════════════════════════════════════════════════════════════"
  
  HPA=$(kubectl get hpa test-app -o jsonpath='{.status.desiredReplicas}' 2>/dev/null || echo "?")
  HPA_CURRENT=$(kubectl get hpa test-app -o jsonpath='{.status.currentReplicas}' 2>/dev/null || echo "?")
  
  PPA_STATUS=$(get_ppa_status)
  PPA=$(echo "$PPA_STATUS" | cut -d'|' -f1)
  PPA_CURRENT=$(echo "$PPA_STATUS" | cut -d'|' -f2)
  PPA_LAST_PREDICTED=$(echo "$PPA_STATUS" | cut -d'|' -f3)
  
  ACTUAL=$(kubectl get deployment test-app -o jsonpath='{.status.replicas}' 2>/dev/null || echo "?")
  READY=$(kubectl get deployment test-app -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "?")
  
  echo "  HPA         : current=$HPA_CURRENT  desired=$HPA"
  echo "  PPA         : current=$PPA_CURRENT  desired=$PPA"
  echo "  Deployment  : replicas=$ACTUAL     ready=$READY"
  
  # ────────────────────────────────────────────────────────────────────
  # SECTION 2: REAL-TIME METRICS
  # ────────────────────────────────────────────────────────────────────
  info_msg "\n📈 REAL-TIME METRICS (now)"
  log_msg "════════════════════════════════════════════════════════════"
  
  CURRENT_RPS=$(query_prometheus 'sum(rate(http_requests_total{pod=~"test-app.*"}[1m]))')
  RPS_PER_REPLICA=$(query_prometheus 'sum(rate(http_requests_total{pod=~"test-app.*"}[1m]))/sum(kube_deployment_status_replicas_ready{deployment="test-app",namespace="default"})')
  CPU=$(query_prometheus 'sum(rate(container_cpu_usage_seconds_total{pod=~"test-app.*"}[1m]))/sum(kube_pod_container_resource_limits{resource="cpu",pod=~"test-app.*"})*100')
  P95=$(query_prometheus 'histogram_quantile(0.95,sum(rate(http_request_duration_seconds_bucket{pod=~"test-app.*"}[1m]))by(le))*1000')
  
  echo "  RPS (total)     : $CURRENT_RPS req/s"
  echo "  RPS/replica     : $RPS_PER_REPLICA req/s"
  echo "  CPU util        : $CPU%"
  echo "  P95 latency     : ${P95} ms"
  
  # ────────────────────────────────────────────────────────────────────
  # SECTION 3: PPA PREDICTION TRACKING
  # ────────────────────────────────────────────────────────────────────
  info_msg "\n🔮 PPA PREDICTION (t+10 minutes ahead)"
  log_msg "════════════════════════════════════════════════════════════"
  
  PPA_PRED_RPS=$(get_ppa_predicted_rps_from_logs)
  HPA_CPU=$(kubectl get hpa test-app -o jsonpath='{.status.currentMetrics[0].resource.current.averageUtilization}' 2>/dev/null || echo "?")
  
  echo "  PPA last predicted load  : $PPA_LAST_PREDICTED req/s"
  echo "  PPA latest observation   : $PPA_PRED_RPS req/s (from logs)"
  echo "  PPA desired replicas     : $PPA"
  echo "  HPA CPU (trigger=50%)    : $HPA_CPU%"
  
  # Store prediction for later validation (t+10)
  if [[ -n "$CURRENT_RPS" && "$CURRENT_RPS" != "N/A" && "$CURRENT_RPS" != "?" ]]; then
    if [[ -n "$PPA_LAST_PREDICTED" && "$PPA_LAST_PREDICTED" != "N/A" && "$PPA_LAST_PREDICTED" != "?" ]]; then
      echo "$TIMESTAMP_EPOCH|$CURRENT_RPS|$PPA_LAST_PREDICTED|$PPA" >> "$PREDICTIONS_FILE"
    fi
  fi
  
  # ────────────────────────────────────────────────────────────────────
  # SECTION 4: VALIDATION (t+10 Check)
  # ────────────────────────────────────────────────────────────────────
  info_msg "\n✅ VALIDATION - Comparing predictions from 10 minutes ago"
  log_msg "════════════════════════════════════════════════════════════"
  
  TEN_MIN_AGO=$((TIMESTAMP_EPOCH - 600))
  VALIDATION_COUNT=0
  TOTAL_ERROR=0
  
  while IFS='|' read -r pred_time pred_rps_observed pred_rps_predicted pred_replicas; do
    if [[ -z "$pred_time" ]]; then continue; fi
    
    TIME_DIFF=$((TIMESTAMP_EPOCH - pred_time))
    
    # Check if prediction is 9-11 minutes old (±1 min tolerance)
    if [[ $TIME_DIFF -ge 540 && $TIME_DIFF -le 660 ]]; then
      ACTUAL_RPS_NOW=$(query_prometheus 'sum(rate(http_requests_total{pod=~"test-app.*"}[1m]))')
      
      if [[ "$ACTUAL_RPS_NOW" != "N/A" && "$ACTUAL_RPS_NOW" != "?" && "$ACTUAL_RPS_NOW" != "" && "$pred_rps_predicted" != "?" ]]; then
        # Calculate error: (actual - predicted) / predicted * 100
        ERROR=$(python3 -c "
try:
    actual = float('$ACTUAL_RPS_NOW')
    predicted = float('$pred_rps_predicted')
    if predicted != 0:
        error = ((actual - predicted) / predicted) * 100
        print(round(error, 2))
    else:
        print('0')
except:
    print('?')
" 2>/dev/null || echo "?")
        
        # Calculate accuracy: 100 - |error|
        if [[ "$ERROR" != "?" ]]; then
          ACCURACY=$(python3 -c "print(round(100 - abs(float($ERROR)), 2))" 2>/dev/null || echo "?")
        else
          ACCURACY="?"
        fi
        
        PRED_TIME=$(date -d @$pred_time '+%H:%M:%S' 2>/dev/null || echo "?")
        
        success_msg "  ✓ Prediction from ${PRED_TIME} (${TIME_DIFF}s ago)"
        echo "    RPS observed at that time  : $pred_rps_observed req/s"
        echo "    Predicted for now (t+10)   : $pred_rps_predicted req/s"
        echo "    Actual RPS now             : $ACTUAL_RPS_NOW req/s"
        echo "    Error                      : $ERROR%"
        echo "    Accuracy                   : $ACCURACY%"
        echo ""
        
        # Log validation
        echo "$TIMESTAMP,$pred_rps_predicted,$ACTUAL_RPS_NOW,$ERROR,$ACCURACY" >> "$PREDICTION_LOG"
        
        VALIDATION_COUNT=$((VALIDATION_COUNT + 1))
      fi
    fi
  done < "$PREDICTIONS_FILE"
  
  if [[ $VALIDATION_COUNT -eq 0 ]]; then
    warn_msg "  ⏳ No validation data yet"
    log_msg "  (Script must run for 10+ minutes to validate t+10 predictions)"
  fi
  
  # ────────────────────────────────────────────────────────────────────
  # SECTION 5: COMPARISON & WINNER
  # ────────────────────────────────────────────────────────────────────
  info_msg "\n🏆 SCALING DECISION COMPARISON"
  log_msg "════════════════════════════════════════════════════════════"
  
  if [[ "$HPA" != "?" && "$PPA" != "?" ]]; then
    HPA_INT=$(echo $HPA | cut -d. -f1)
    PPA_INT=$(echo $PPA | cut -d. -f1)
    
    if [[ $HPA_INT -gt $PPA_INT ]]; then
      warn_msg "  ⚠️  HPA is MORE CONSERVATIVE (wants $HPA_INT replicas)"
      log_msg "  → HPA scales UP more than PPA (CPU-triggered)"
      log_msg "  → PPA scales DOWN more (RPS prediction lower)"
    elif [[ $PPA_INT -gt $HPA_INT ]]; then
      success_msg "  ✓ PPA is MORE CONSERVATIVE (wants $PPA_INT replicas)"
      log_msg "  → PPA scales UP more than HPA (RPS forecast higher)"
      log_msg "  → HPA scales DOWN more (CPU lower than threshold)"
    else
      echo "  🤝 AGREEMENT (both want $HPA_INT replicas)"
    fi
  fi
  
  echo ""
  log_msg "  Actual running: $ACTUAL replicas"
  
  # ────────────────────────────────────────────────────────────────────
  # SECTION 6: STATS & LOGS
  # ────────────────────────────────────────────────────────────────────
  info_msg "\n📊 OVERALL PREDICTION ACCURACY"
  log_msg "════════════════════════════════════════════════════════════"
  
  TOTAL_VALIDATIONS=$(tail -n +2 "$PREDICTION_LOG" 2>/dev/null | wc -l)
  
  if [[ $TOTAL_VALIDATIONS -gt 0 ]]; then
    AVG_ACCURACY=$(tail -n +2 "$PREDICTION_LOG" | awk -F, '{sum+=$5; count++} END {if(count>0) print int(sum/count); else print 0}')
    AVG_ERROR=$(tail -n +2 "$PREDICTION_LOG" | awk -F, '{if($4 ~ /^-/) val=-$4; else val=$4; sum+=val; count++} END {if(count>0) print int(sum/count); else print 0}')
    
    echo "  Total validations : $TOTAL_VALIDATIONS"
    echo "  Avg accuracy      : $AVG_ACCURACY%"
    echo "  Avg error (abs)   : ±$AVG_ERROR%"
    
    if [[ $AVG_ACCURACY -ge 90 ]]; then
      success_msg "  ✓ PPA predictions are HIGHLY ACCURATE"
    elif [[ $AVG_ACCURACY -ge 80 ]]; then
      log_msg "  ✓ PPA predictions are GOOD"
    else
      warn_msg "  ⚠ PPA predictions need tuning"
    fi
  else
    log_msg "  (Waiting for 10 min to complete first validation cycle)"
  fi
  
  info_msg "\n📝 RECENT SCALING EVENTS"
  log_msg "════════════════════════════════════════════════════════════"
  
  kubectl logs deployment/ppa-operator --tail=5 2>/dev/null | \
    grep -E "Predicted load|Scaling|Patched" | \
    sed 's/^/  /' || log_msg "  (no recent events)"
  
  # ────────────────────────────────────────────────────────────────────
  # FOOTER
  # ────────────────────────────────────────────────────────────────────
  info_msg "\n📂 DATA FILES"
  log_msg "════════════════════════════════════════════════════════════"
  echo "  Predictions  : $PREDICTIONS_FILE ($(wc -l < $PREDICTIONS_FILE) entries)"
  echo "  Validations  : $PREDICTION_LOG ($(tail -n +2 $PREDICTION_LOG | wc -l) entries)"
  
  log_msg ""
  log_msg "Press Ctrl+C to stop | Next update in 15 seconds..."
  
  sleep 15
done