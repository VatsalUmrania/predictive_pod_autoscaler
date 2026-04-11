apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata:
  name: ${APP_NAME}-ppa-${HORIZON_CLEAN}
  namespace: ${NAMESPACE}
spec:
  targetDeployment: "${TARGET_DEPLOYMENT}"
  appName: "${APP_NAME}"
  horizon: "${HORIZON}"
  minReplicas: ${MIN_REPLICAS}
  maxReplicas: ${MAX_REPLICAS}
  capacityPerPod: ${RPS_CAPACITY}
  scaleUpRate: ${SCALE_UP_RATE}
  scaleDownRate: ${SCALE_DOWN_RATE}
  safetyFactor: ${SAFETY_FACTOR}
  observerMode: ${OBSERVER_MODE}
  modelPath: "/models/${APP_NAME}/${HORIZON}/ppa_model_${HORIZON}.tflite"
  scalerPath: "/models/${APP_NAME}/${HORIZON}/scaler_${HORIZON}.pkl"
  targetScalerPath: "/models/${APP_NAME}/${HORIZON}/target_scaler_${HORIZON}.pkl"
