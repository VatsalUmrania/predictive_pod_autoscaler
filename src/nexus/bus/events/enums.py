"""Enumerations for NEXUS Incident Events."""

from __future__ import annotations

from enum import Enum


class SignalSource(str, Enum):
    AWS_CLOUDWATCH = "aws_cloudwatch"
    AWS_EVENTBRIDGE = "aws_eventbridge"
    KUBERNETES_API = "kubernetes_api"
    PROMETHEUS = "prometheus"
    OTEL_SDK = "otel_sdk"
    WEBHOOK = "webhook"


class SignalKind(str, Enum):
    METRIC_ANOMALY = "metric_anomaly"
    ERROR_LOG_BURST = "error_log_burst"
    STATE_TRANSITION = "state_transition"
    SLO_BURN_ALERT = "slo_burn_alert"


class AgentType(str, Enum):
    NGINX = "nginx"
    GIT = "git"
    METRICS = "metrics"
    DB = "db"
    NETWORK = "network"
    CONFIG = "config"
    K8S = "k8s"
    ORCHESTRATOR = "orchestrator"  # for system-level internal events
    # ── AWS Serverless Agents ─────────────────────────────────────────────────
    LAMBDA = "lambda"
    APIGW = "apigw"
    SQS = "sqs"
    DYNAMODB = "dynamodb"
    CLOUDWATCH = "cloudwatch"  # catch-all CloudWatch Alarm agent


class SignalType(str, Enum):
    # ── MetricsAgent ──────────────────────────────────────────────────────────
    ANOMALY_DETECTED = "anomaly_detected"
    THRESHOLD_BREACH = "threshold_breach"
    METRIC_UNAVAILABLE = "metric_unavailable"
    CIRCUIT_BREAKER_TRIPPED = "circuit_breaker_tripped"

    # ── GitAgent ───────────────────────────────────────────────────────────────
    DEPLOY_EVENT = "deploy_event"
    DEPLOY_BLOCKED = "deploy_blocked"
    ENV_CONTRACT_VIOLATION = "env_contract_violation"
    SECRET_COMMITTED = "secret_committed"
    ROLLBACK_SUGGESTED = "rollback_suggested"

    # ── K8sAgent ──────────────────────────────────────────────────────────────
    POD_CRASHLOOP = "pod_crashloop"
    POD_OOMKILLED = "pod_oomkilled"
    POD_PENDING = "pod_pending"
    DEPLOYMENT_DEGRADED = "deployment_degraded"
    ROLLOUT_STUCK = "rollout_stuck"
    HPA_MAXED = "hpa_maxed"

    # ── NginxAgent ────────────────────────────────────────────────────────────
    HIGH_ERROR_RATE = "high_error_rate"
    HIGH_LATENCY = "high_latency"
    UPSTREAM_DOWN = "upstream_down"
    TRAFFIC_SPIKE = "traffic_spike"

    # ── DBAgent ───────────────────────────────────────────────────────────────
    DB_CONNECTION_EXHAUSTION = "db_connection_exhaustion"
    SLOW_QUERY_DETECTED = "slow_query_detected"
    DB_QUERY_SPIKE = "db_query_spike"
    DB_LOCK_CONTENTION = "db_lock_contention"
    DB_REPLICATION_LAG = "db_replication_lag"

    # ── NetworkAgent ──────────────────────────────────────────────────────────
    DNS_RESOLUTION_FAILURE = "dns_resolution_failure"
    SERVICE_UNREACHABLE = "service_unreachable"
    NETWORK_PARTITION = "network_partition"
    INTER_SERVICE_LATENCY = "inter_service_latency_high"

    # ── ConfigAgent ───────────────────────────────────────────────────────────
    CONFIG_DRIFT = "config_drift"
    ENV_KEY_MISSING = "env_key_missing"
    SECRET_MISMATCH = "secret_mismatch"
    IAC_DRIFT = "iac_drift"

    # ── Predictive Layer ──────────────────────────────────────────────────────
    TRAFFIC_SPIKE_PREDICTED = "traffic_spike_predicted"
    ANOMALY_PREDICTED = "anomaly_predicted"

    # ── AWS Lambda ───────────────────────────────────────────────────────────
    LAMBDA_ERROR_RATE_HIGH = "lambda_error_rate_high"
    LAMBDA_THROTTLE_SPIKE = "lambda_throttle_spike"
    LAMBDA_TIMEOUT = "lambda_timeout"
    LAMBDA_OOM = "lambda_oom"
    LAMBDA_COLD_START_HIGH = "lambda_cold_start_high"
    LAMBDA_CONCURRENCY_MAXED = "lambda_concurrency_maxed"

    # ── AWS API Gateway ───────────────────────────────────────────────────────
    APIGW_5XX_SPIKE = "apigw_5xx_spike"
    APIGW_LATENCY_HIGH = "apigw_latency_high"
    APIGW_4XX_SPIKE = "apigw_4xx_spike"

    # ── AWS SQS ──────────────────────────────────────────────────────────────
    SQS_DLQ_DEPTH_HIGH = "sqs_dlq_depth_high"
    SQS_QUEUE_DEPTH_HIGH = "sqs_queue_depth_high"
    SQS_CONSUMER_LAG = "sqs_consumer_lag"

    # ── AWS DynamoDB ──────────────────────────────────────────────────────────
    DYNAMO_THROTTLE_READ = "dynamo_throttle_read"
    DYNAMO_THROTTLE_WRITE = "dynamo_throttle_write"
    DYNAMO_SYSTEM_ERROR = "dynamo_system_error"
    DYNAMO_CONSUMED_RCU_HIGH = "dynamo_consumed_rcu_high"

    # ── AWS CloudWatch (catch-all) ─────────────────────────────────────────────
    AWS_ALARM_FIRED = "aws_alarm_fired"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
    INFO = "info"
    WARNING = "warning"
    EMERGENCY = "emergency"


class HealingLevel(int, Enum):
    """Maps to the 4-level Healing Actions Ladder in the Governance Plane."""

    L0_ALERT_ONLY = 0  # Detect + alert; no infra changes
    L1_NO_REGRET = 1  # Pod restart, flush cache, scale +1 — always safe
    L2_BOUNDED_MITIGATION = 2  # Canary shift, rate-limit, drain — bounded blast radius
    L3_HIGH_RISK = (
        3  # Rollback, config patch, DNS mutation — human approval if confidence < 0.85
    )
