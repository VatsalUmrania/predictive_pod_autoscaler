-- ============================================================================
-- NEXUS Unified Agent & Incident Response PostgreSQL Schema
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Enums (created safely if not exists)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'incident_environment') THEN
        CREATE TYPE incident_environment AS ENUM ('kubernetes', 'aws', 'hybrid', 'on_prem');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'incident_severity') THEN
        CREATE TYPE incident_severity AS ENUM ('info', 'warning', 'error', 'critical');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'incident_lifecycle_state') THEN
        CREATE TYPE incident_lifecycle_state AS ENUM (
            'detected', 'correlated', 'diagnosing', 'planning',
            'policy_check', 'approval_pending', 'executing',
            'verifying', 'resolved', 'retrying', 'rolling_back',
            'rolled_back', 'rejected', 'escalated', 'failed'
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'agent_run_status') THEN
        CREATE TYPE agent_run_status AS ENUM ('running', 'completed', 'failed', 'timed_out', 'cancelled');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'message_role') THEN
        CREATE TYPE message_role AS ENUM ('system', 'user', 'assistant', 'tool');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'tool_status') THEN
        CREATE TYPE tool_status AS ENUM ('invoked', 'success', 'failed', 'denied', 'timed_out');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'tool_category') THEN
        CREATE TYPE tool_category AS ENUM ('k8s_read', 'k8s_mutate', 'aws_read', 'aws_mutate', 'system');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'action_level') THEN
        CREATE TYPE action_level AS ENUM ('L0_OBSERVE', 'L1_SAFE_AUTOMATED', 'L2_MUTATING_APPROVAL', 'L3_DESTRUCTIVE');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'action_outcome') THEN
        CREATE TYPE action_outcome AS ENUM ('pending', 'in_progress', 'success', 'failed', 'rolled_back', 'skipped');
    END IF;
END$$;

-- 1. Incidents Table
CREATE TABLE IF NOT EXISTS incidents (
    incident_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    fingerprint         VARCHAR(255) NOT NULL,
    environment         incident_environment NOT NULL,
    target_resource     VARCHAR(255) NOT NULL,
    severity            incident_severity NOT NULL DEFAULT 'error',
    current_state       incident_lifecycle_state NOT NULL DEFAULT 'detected',
    trigger_source      VARCHAR(100) NOT NULL,
    trigger_payload     JSONB NOT NULL DEFAULT '{}',
    root_cause_summary  TEXT,
    confidence_score    NUMERIC(4, 3),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint ON incidents(fingerprint);
CREATE INDEX IF NOT EXISTS idx_incidents_state ON incidents(current_state);
CREATE INDEX IF NOT EXISTS idx_incidents_created ON incidents(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_target ON incidents(target_resource);

-- 2. State Transitions
CREATE TABLE IF NOT EXISTS incident_state_transitions (
    transition_id   BIGSERIAL PRIMARY KEY,
    incident_id     UUID NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
    from_state      incident_lifecycle_state NOT NULL,
    to_state        incident_lifecycle_state NOT NULL,
    reason          TEXT,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transitions_incident ON incident_state_transitions(incident_id);
CREATE INDEX IF NOT EXISTS idx_transitions_created ON incident_state_transitions(created_at DESC);

-- 3. Agent Runs
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    incident_id         UUID NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
    agent_type          VARCHAR(50) NOT NULL,
    model_name          VARCHAR(100) NOT NULL,
    status              agent_run_status NOT NULL DEFAULT 'running',
    prompt_tokens       INTEGER DEFAULT 0,
    completion_tokens   INTEGER DEFAULT 0,
    total_tokens        INTEGER DEFAULT 0,
    duration_ms         INTEGER,
    error_message       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_incident ON agent_runs(incident_id);

-- 4. Agent Messages
CREATE TABLE IF NOT EXISTS agent_messages (
    message_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id              UUID NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    sequence_num        INTEGER NOT NULL,
    role                message_role NOT NULL,
    content             TEXT,
    reasoning_content   TEXT,
    raw_payload         JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_run_sequence UNIQUE (run_id, sequence_num)
);

CREATE INDEX IF NOT EXISTS idx_agent_messages_run ON agent_messages(run_id);

-- 5. Tool Invocations
CREATE TABLE IF NOT EXISTS tool_calls (
    call_id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id              UUID NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    message_id          UUID REFERENCES agent_messages(message_id) ON DELETE SET NULL,
    tool_name           VARCHAR(100) NOT NULL,
    tool_category       tool_category NOT NULL,
    input_parameters    JSONB NOT NULL DEFAULT '{}',
    output_result       JSONB,
    status              tool_status NOT NULL DEFAULT 'invoked',
    error_details       TEXT,
    execution_duration_ms INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_name ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_calls_status ON tool_calls(status);

-- 6. Remediation Actions & Governance
CREATE TABLE IF NOT EXISTS remediation_actions (
    action_id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    incident_id         UUID NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
    run_id              UUID REFERENCES agent_runs(run_id) ON DELETE SET NULL,
    action_name         VARCHAR(100) NOT NULL,
    action_level        action_level NOT NULL,
    target_resource     VARCHAR(255) NOT NULL,
    parameters          JSONB NOT NULL DEFAULT '{}',
    rollback_plan       JSONB,
    policy_check_passed BOOLEAN NOT NULL DEFAULT FALSE,
    policy_evaluation   JSONB,
    human_approved_by   VARCHAR(100),
    approval_granted_at TIMESTAMPTZ,
    pre_check_snapshot  JSONB,
    post_check_snapshot JSONB,
    outcome             action_outcome NOT NULL DEFAULT 'pending',
    retry_attempt       INTEGER DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    executed_at         TIMESTAMPTZ,
    verified_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_remediation_incident ON remediation_actions(incident_id);
CREATE INDEX IF NOT EXISTS idx_remediation_outcome ON remediation_actions(outcome);
