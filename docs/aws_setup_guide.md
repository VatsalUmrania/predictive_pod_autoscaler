# AWS Setup Guide — NEXUS AI DevOps Agent

> **Goal**: Deploy the AI agent and a sample failing app on AWS, trigger failures, and watch the agent diagnose and fix them automatically.

---

## Prerequisites Checklist

| Tool | Version | Install |
|---|---|---|
| AWS CLI | v2+ | [docs.aws.amazon.com/cli](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) |
| Terraform | v1.5+ | [developer.hashicorp.com/terraform](https://developer.hashicorp.com/terraform/install) |
| Python | 3.12 | [python.org](https://www.python.org/downloads/) |
| Git | any | [git-scm.com](https://git-scm.com/) |
| Anthropic API Key | — | [console.anthropic.com](https://console.anthropic.com/) |
| Slack Webhook | — | [api.slack.com/messaging/webhooks](https://api.slack.com/messaging/webhooks) (optional) |

---

## Step 1 — AWS Account Setup

### 1.1 Create an IAM User for Terraform

In AWS Console → **IAM** → **Users** → **Create User**:

```
Username:    nexus-terraform-user
Access type: Programmatic access (generate access keys)
```

Attach this policy directly (or create a custom one):
```
AdministratorAccess   ← For initial setup only
```

> [!IMPORTANT]
> After everything is deployed and working, **replace AdministratorAccess** with a least-privilege custom policy. AdministratorAccess is only for the initial Terraform provisioning.

### 1.2 Configure AWS CLI

```bash
aws configure
# AWS Access Key ID:     [from IAM user]
# AWS Secret Access Key: [from IAM user]
# Default region:        us-east-1
# Default output format: json
```

Verify:
```bash
aws sts get-caller-identity
# Should return your account ID and IAM user ARN
```

---

## Step 2 — Get an Anthropic API Key

1. Go to [console.anthropic.com](https://console.anthropic.com/)
2. **API Keys** → **Create Key**
3. Copy the key — you'll need it for Terraform and `.env`

The agent uses **Claude Sonnet** (default: `claude-sonnet-4-5`).

---

## Step 3 — Set Up Slack Notifications (Optional but Recommended)

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From Scratch**
2. Name: `NEXUS AI Agent`, pick your workspace
3. **Incoming Webhooks** → **Activate** → **Add New Webhook to Workspace**
4. Pick a channel (e.g. `#alerts`)
5. Copy the webhook URL: `https://hooks.slack.com/services/T.../B.../...`

The agent sends:
- 🔔 Alert when an incident is detected
- 🔧 Update when it takes action
- ✅ Resolution when the incident clears
- 🆘 Escalation request when confidence < 85%

---

## Step 4 — Configure Environment Variables

```bash
cp .env.example .env
```

Edit `.env`:
```bash
ANTHROPIC_API_KEY=sk-ant-...          # Required
SLACK_WEBHOOK_URL=https://hooks...    # Recommended
DEFAULT_REGION=us-east-1
AGENT_DRY_RUN=true                    # Start with dry-run for safety!
AGENT_CONFIDENCE_THRESHOLD=0.85
AGENT_VERIFY_WAIT_SECONDS=120
```

> [!TIP]
> Set `AGENT_DRY_RUN=true` for your first deployment. The agent will log all actions but not modify any AWS resources. Switch to `false` once you've verified the diagnoses look correct.

---

## Step 5 — Package the Lambda

The agent Lambda needs its dependencies bundled into a zip file.

**On Linux/macOS:**
```bash
bash scripts/package_lambda.sh
```

**On Windows (PowerShell):**
```powershell
# Install deps to lambda_package/
pip install -r requirements-aws-agent.txt -t lambda_package --no-cache-dir

# Copy source code
xcopy /E /I src\aws lambda_package\aws

# Create zip (requires 7-Zip or PowerShell compression)
Compress-Archive -Path lambda_package\* -DestinationPath agent.zip -Force
```

Expected output: `agent.zip` (~15-30 MB depending on dependencies)

---

## Step 6 — Deploy with Terraform

```bash
cd infra/terraform

# Initialize Terraform (download AWS provider)
terraform init

# Preview what will be created
terraform plan \
  -var="anthropic_api_key=sk-ant-..." \
  -var="slack_webhook_url=https://hooks.slack.com/..." \
  -var="agent_dry_run=true"

# Deploy everything
terraform apply \
  -var="anthropic_api_key=sk-ant-..." \
  -var="slack_webhook_url=https://hooks.slack.com/..." \
  -var="agent_dry_run=true"
```

Type `yes` when prompted.

**What gets created** (~25 resources):
```
Lambda:     nexus-ai-agent-function     ← AI agent
Lambda:     nexus-ai-agent-sample-app  ← test victim
API GW:     nexus-ai-agent-sample-api  ← HTTP trigger
DynamoDB:   nexus-ai-agent-sample-table
DynamoDB:   nexus-ai-agent-incidents   ← Phase 9 memory
SQS:        nexus-ai-agent-sample-queue + DLQ
CloudWatch: 5 alarms
EventBridge: 1 rule → agent Lambda
IAM:        2 roles + 2 policies
```

Save the outputs:
```bash
terraform output
# Note: sample_api_endpoint and test_commands
```

---

## Step 7 — Test Each Failure Type

### 7.1 Lambda Error Rate (most common)
```bash
# Trigger 10 errors → CloudWatch will ALARM after 1 minute
for i in {1..10}; do
  aws lambda invoke \
    --function-name nexus-ai-agent-sample-app \
    --cli-binary-format raw-in-base64-out \
    --payload '{"path":"/fail/error"}' \
    /dev/null
done

# Watch the agent logs
aws logs tail /aws/lambda/nexus-ai-agent-function --follow
```

### 7.2 Lambda Timeout
```bash
aws lambda invoke \
  --function-name nexus-ai-agent-sample-app \
  --cli-binary-format raw-in-base64-out \
  --payload '{"path":"/fail/timeout","queryStringParameters":{"sleep":"10"}}' \
  response.json
# Lambda timeout is 5s, sleep is 10s → guaranteed timeout
```

### 7.3 DLQ Fill (SQS Dead Letter Queue)
```bash
DLQ_URL=$(terraform output -raw sample_dlq_url)

aws lambda invoke \
  --function-name nexus-ai-agent-sample-app \
  --cli-binary-format raw-in-base64-out \
  --payload "{\"path\":\"/fail/dlq\",\"queryStringParameters\":{\"count\":\"25\",\"queue_url\":\"$(terraform output -raw sample_queue_url)\"}}" \
  response.json

# Check DLQ depth
aws sqs get-queue-attributes \
  --queue-url $DLQ_URL \
  --attribute-names ApproximateNumberOfMessages
```

### 7.4 DynamoDB Throttle
```bash
aws lambda invoke \
  --function-name nexus-ai-agent-sample-app \
  --cli-binary-format raw-in-base64-out \
  --payload '{"path":"/fail/throttle","queryStringParameters":{"count":"200"}}' \
  response.json
```

### 7.5 Via HTTP API (API Gateway)
```bash
API_URL=$(terraform output -raw sample_api_endpoint)

curl -X POST $API_URL/fail/error
curl -X POST $API_URL/fail/timeout
curl $API_URL/health
```

### 7.6 Invoke the Agent Directly (no alarm needed)
```bash
aws lambda invoke \
  --function-name nexus-ai-agent-function \
  --payload '{
    "resource_name": "nexus-ai-agent-sample-app",
    "signal_type":   "lambda_error_rate_high",
    "raw_metrics":   {}
  }' \
  agent_response.json

cat agent_response.json | python -m json.tool
```

---

## Step 8 — Monitor and Verify

### Watch agent logs in real time
```bash
aws logs tail /aws/lambda/nexus-ai-agent-function --follow
```

### Check CloudWatch Alarms
```bash
aws cloudwatch describe-alarms \
  --alarm-name-prefix nexus-ai-agent \
  --query "MetricAlarms[*].{Name:AlarmName,State:StateValue}"
```

### View incident memory (DynamoDB)
```bash
aws dynamodb scan \
  --table-name nexus-ai-agent-incidents \
  --query "Items[*].{resource:resource_name.S,action:action_taken.S,resolved:resolved.BOOL}" \
  --output table
```

### CloudWatch Metrics Dashboard
1. AWS Console → **CloudWatch** → **Dashboards** → **Create Dashboard**
2. Add widgets for:
   - Lambda `Errors` metric for both functions
   - SQS `ApproximateNumberOfMessagesVisible` for the DLQ
   - DynamoDB `WriteThrottleEvents`

---

## Step 9 — Enable Auto-Remediation

Once you're satisfied with the dry-run diagnoses:

```bash
cd infra/terraform

terraform apply \
  -var="anthropic_api_key=sk-ant-..." \
  -var="slack_webhook_url=https://hooks.slack.com/..." \
  -var="agent_dry_run=false"   # ← Remove dry-run
```

> [!WARNING]
> With `agent_dry_run=false`, the agent WILL modify AWS resources.
> Specifically: it can increase Lambda memory/timeout, roll back aliases, set concurrency limits, and replay DLQ messages.
> Make sure you trust the diagnoses before enabling this.

---

## Step 10 — Verify End-to-End Flow

Trigger a Lambda error and watch the full pipeline:

```bash
# Terminal 1: Watch agent logs
aws logs tail /aws/lambda/nexus-ai-agent-function --follow

# Terminal 2: Trigger errors
for i in {1..15}; do
  aws lambda invoke \
    --function-name nexus-ai-agent-sample-app \
    --payload '{"path":"/fail/error"}' /dev/null
  sleep 1
done
```

**Expected sequence** (within 2-3 minutes):
```
[CloudWatch]  Errors alarm → ALARM state
[EventBridge] Routes alarm to agent Lambda
[Agent]       collect  → gathering context...
[Agent]       diagnose → Claude Sonnet analyzing...
[Agent]       policy   → confidence=0.XX approved=True
[Agent]       execute  → increasing memory (if dry_run=false)
[Agent]       verify   → waiting 120s...
[Agent]       verify   → error rate = 0.00 ✅ resolved
[Slack]       "Incident RESOLVED: lambda_error_rate_high on nexus-ai-agent-sample-app"
```

---

## Cleanup

To destroy all AWS resources:
```bash
cd infra/terraform
terraform destroy \
  -var="anthropic_api_key=placeholder" \
  -var="slack_webhook_url=placeholder"
```

> [!CAUTION]
> This will delete all DynamoDB incident memory, SQS queues, and Lambda functions permanently.

---

## Cost Estimate

| Resource | Monthly Cost (estimate) |
|---|---|
| Lambda (agent, ~100 invocations) | ~$0.001 |
| Lambda (sample app, testing) | ~$0.00 |
| DynamoDB (on-demand, few writes) | ~$0.01 |
| CloudWatch (5 alarms, logs) | ~$2-5 |
| API Gateway (few requests) | ~$0.00 |
| SQS (few messages) | ~$0.00 |
| **Total** | **~$2-5/month** |

The agent only runs when an alarm fires — there's no polling loop.

---

## Common Issues

| Problem | Cause | Fix |
|---|---|---|
| `Invalid ANTHROPIC_API_KEY` | Wrong key | Check `.env` or Terraform var |
| Agent Lambda timeout | `AGENT_VERIFY_WAIT_SECONDS` > Lambda timeout | Lambda timeout must be > verify wait + 60s |
| No alarms firing | CloudWatch metrics have 1-min delay | Wait 2 minutes after triggering errors |
| `ResourceNotFoundException` on DynamoDB | Table not created yet | Check terraform apply output |
| Agent logs show `dry_run=True` | Expected! | Set `agent_dry_run=false` in Terraform var |
| `AccessDeniedException` | IAM role missing permission | Check IAM policy in `main.tf` |
