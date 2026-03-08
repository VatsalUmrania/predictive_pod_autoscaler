#!/bin/bash

# fixed_replica_test.sh
# Automate running load tests with the Horizontal Pod Autoscaler disabled
# Scales deployment to user-specified fixed replicas (2, 5, 10, 20) and logs training correlations

NAMESPACE="default"
DEPLOYMENT="test-app"
REPLICAS=(2 5 10 20)
TEST_DURATION="15m"
LOCUST_FILE="tests/locustfile.py"
LOCUST_HOST="http://localhost:8080" # modify based on cluster ingress/port forwarding

echo "Initializing Fixed Replica Test Scenarios"
echo "Disabling HPA for deployment $DEPLOYMENT in namespace $NAMESPACE..."

# Temporarily delete HPA to prevent autoscaling overriding our fixed scenarios
kubectl delete hpa $DEPLOYMENT -n $NAMESPACE --ignore-not-found

for rep in "${REPLICAS[@]}"; do
    echo "======================================"
    echo "Scaling $DEPLOYMENT to $rep replicas..."
    kubectl scale deployment $DEPLOYMENT -n $NAMESPACE --replicas=$rep
    
    # Wait for pods to become ready
    kubectl rollout status deployment/$DEPLOYMENT -n $NAMESPACE
    
    echo "Running Chaotic Load Test Profile for $TEST_DURATION on $rep replicas"
    
    # Run locust headlessly with ChaoticLoadShape
    # Real-time mode recommended for high-fidelity metrics
    STAGE_CHAOS_MED=10 locust -f $LOCUST_FILE --headless \
      --host $LOCUST_HOST \
      --run-time $TEST_DURATION \
      --csv="data-collection/training-data/fixed_rep_${rep}_"
      
    echo "Completed Fixed Replica test for $rep replicas."
    sleep 30 # Let metrics flush into Prometheus
done

echo "All fixed replica scenarios completed. Re-enabling HPA is recommended."
