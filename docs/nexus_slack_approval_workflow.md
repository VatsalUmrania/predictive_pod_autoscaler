# NEXUS → Slack Human-Approval Workflow

Human-in-the-loop approval for L3 self-healing actions, wired to Slack
interactive messages with **Approve ✅** / **Reject ❌** buttons. A button
click POSTs to a backend webhook (`/slack/interactive`) that verifies the
Slack request signature and routes the decision through NEXUS's existing
governance plane.

This is already implemented in `src/nexus/` — this doc is the runbook for
configuring Slack + ngrok and exercising both paths.

---

## 1. Where the code lives

| Concern | File | Notes |
|---|---|---|
| Approval queue (enqueue/approve/reject) | `src/nexus/governance/action_ladder.py` → `HumanApprovalQueue` | In-memory staging; one `approval_id` (8-char) per action |
| Slack message **with Approve/Reject buttons** | `src/nexus/integration/notifier.py` → `Notifier.notify_approval_required` + `_approval_buttons_block` | Block Kit `actions` block; gated on `SLACK_INTERACTIVE_URL` |
| Queue → NATS → Notifier bridge | `HumanApprovalQueue.enqueue` publishes `nexus.approvals.required`; `Notifier._listen` consumes it | fire-and-forget; the buttons only render when `SLACK_INTERACTIVE_URL` is set |
| LLM-path approval request | `src/nexus/integration/chatops.py` → `send_approval_request` | alternate sender for LLM-sourced remediations |
| Webhook handler + **signature verification** | `src/nexus/observability/status_api.py` → `POST /slack/interactive` | HMAC-SHA256 `v0` scheme |
| Signature verifier | `src/nexus/integration/notifier.py` → `verify_slack_request` | the trust boundary |
| Re-dispatch through governance | `src/nexus/governance/runbook_executor.py` → `execute_approved` | cooldown / circuit-breaker / OPA / audit / rollback **still apply after approval** |
| Plain HTTP + CLI equivalents | `/{approve,reject}/{id}`, `/approvals/pending`; `nexus approve <id>` / `nexus reject <id>` | same `HumanApprovalQueue` |
| Tests | `tests/unit/nexus/test_slack_interactive.py` | signature gate + approve/reject e2e via httpx ASGITransport |

---

## 2. Slack app configuration

Create an app at <https://api.slack.com/apps> → **Create New App** → **From scratch**.

### 2a. OAuth scopes (Bot Token)
**OAuth & Permissions → Bot Token Scopes →** add:
- `incoming-webhook` — post to a channel via webhook URL
- `chat:write` — (optional) post the approval message as the bot
- `commands` — (optional) if you later add slash-command approvals

Incoming webhooks are the lightest path and are what `Notifier` uses. Install the
app to your workspace/channel to get a **Webhook URL** (`https://hooks.slack.com/services/...`).

### 2b. Interactivity (the button callbacks)
**Features → Interactivity & Shortcuts →** toggle **On**.

- **Request URL:** `https://<your-ngrok-subdomain>.ngrok-free.app/slack/interactive`
- Slack retries this URL up to 3× if it doesn't see HTTP 200 within 3s — so keep
  the handler fast (it is: it records + dispatches a single async action).
- On a successful click, Slack expects **either** a 200 with an empty/special
  JSON body **or** a 200 with `{"replace_original": true, "text": "..."}`. We do
  the latter so the Approve/Reject buttons are replaced by the decision
  (prevents a second operator from acting on a resolved action).

### 2c. App credentials
**Basic Information → App Credentials →** copy:
- **Signing Secret** → `SLACK_SIGNING_SECRET`
- **Webhook URL** (from 2a) → `SLACK_WEBHOOK`

> ⚠️ Do not skip the signing secret. Without it `verify_slack_request` returns
> `False` for *every* request and `/slack/interactive` 401s all clicks
> (secure-by-default). The only way to approve/reject then is the CLI/HTTP path.

---

## 3. Environment variables

| Var | Required | Purpose |
|---|---|---|
| `SLACK_WEBHOOK` | yes | incoming webhook URL — the **global fallback** for all Slack senders (governed notifier + chatops). A per-app `selfheal.yaml` override (`notifications.slack_webhook: ${SLACK_WEBHOOK}`) wins when that app has a policy loaded; otherwise this env var is used. Works without mounting a `selfheal.yaml` |
| `SLACK_SIGNING_SECRET` | yes | app Signing Secret; authenticates click payloads |
| `SLACK_INTERACTIVE_URL` | yes (for buttons) | your public callback URL, e.g. `https://xxx.ngrok-free.app/slack/interactive`. When **unset**, `_approval_buttons_block` returns an empty block and the message falls back to `nexus approve <id>` instructions |
| `NATS_URL` | yes (runtime) | `nats://localhost:4222` — the queue→notifier bridge is over NATS |
| `NEXUS_LLM_API_KEY` | optional | enables Gemini RCA (the path most likely to stage approvals) |

Both Slack senders resolve the webhook through **one** function,
`resolve_slack_webhook(app_name)` in `src/nexus/integration/notifier.py`:
a per-app `selfheal.yaml` override is authoritative (an app loaded in the policy
cache with no `slack_webhook` stays deliberately silent), else the `SLACK_WEBHOOK`
env var is the global fallback. There is no longer a separate `SLACK_WEBHOOK_URL`
— the old chatops-only var was consolidated into this single resolver.

`.env` sketch:
```dotenv
SLACK_WEBHOOK=https://hooks.slack.com/services/T000/B000/XXXXXXXX
SLACK_SIGNING_SECRET=abcdef0123456789...
SLACK_INTERACTIVE_URL=https://your-subdomain.ngrok-free.app/slack/interactive
NATS_URL=nats://localhost:4222
```

---

## 4. Local development with ngrok

Slack can only reach a public HTTPS URL. ngrok tunnels your local port out.

```bash
# 1. expose the NEXUS status API (uvicorn, port 8080) — see server.py
ngrok http 8080 --region us --request-header-add "host: localhost"
```

Copy the `https://<subdomain>.ngrok-free.app` forwarding URL and set:
```bash
export SLACK_INTERACTIVE_URL="https://<subdomain>.ngrok-free.app/slack/interactive"
```
Paste the same value into **Interactivity → Request URL** in the Slack app.

Notes:
- The free plan re-issues the subdomain on restart. For stable dev use a
  reserved domain (`ngrok http 8080 --domain=your-domain.ngrok-free.app`).
- Visit `https://<sub>.ngrok-free.app/health` in a browser once to dismiss
  ngrok's interstitial warning page.
- You can watch every request Slack makes in the ngrok web inspector
  (`http://localhost:4040`) — useful for dumping the raw `payload`.

---

## 5. How an interactive message is sent

`HumanApprovalQueue.enqueue` (called by `ActionLadder.evaluate` for L3 actions
below the confidence gate, and by the Orchestrator for LLM-sourced remediations)
fires a `nexus.approvals.required` NATS event. `Notifier._listen` consumes it and
calls `notify_approval_required`, which builds a Block Kit message and appends
the buttons block:

```python
# src/nexus/integration/notifier.py
def _approval_buttons_block(approval_id: str) -> dict:
    return {
        "type": "actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Approve ✅"},
             "style": "primary", "action_id": "nexus_approve", "value": approval_id},
            {"type": "button", "text": {"type": "plain_text", "text": "Reject ❌"},
             "style": "danger", "action_id": "nexus_reject", "value": approval_id},
        ],
    }
```

`value` carries the `approval_id` so the webhook knows *which* staged action a
click refers to. `action_id` is `nexus_approve` or `nexus_reject`.

For the LLM diagnosis path, `chatops.send_approval_request` builds its own
message using the same `_approval_buttons_block` helper — single source of truth
for the button contract.

---

## 6. Handling the button click + signature validation

Slack POSTs `application/x-www-form-urlencoded` with a `payload` form field
holding JSON. The handler verifies the signature first, then parses the form
body with stdlib `urllib.parse.parse_qs` (no `python-multipart` dependency):

```python
# src/nexus/observability/status_api.py
@app.post("/slack/interactive", tags=["governance"])
async def slack_interactive(request: Request) -> dict[str, Any]:
    from nexus.integration.notifier import verify_slack_request
    from urllib.parse import parse_qs

    raw = await request.body()
    if not verify_slack_request(raw, request.headers):
        raise HTTPException(status_code=401, detail="invalid_signature")

    payload = (parse_qs(raw.decode("utf-8")).get("payload") or [None])[0]
    data = json.loads(payload)
    action = data["actions"][0]
    action_id   = action["action_id"]   # "nexus_approve" | "nexus_reject"
    approval_id = action["value"]       # the staged approval UUID
    user        = data["user"]["username"] or data["user"]["id"]
    ...
```

The verifer (the trust boundary) is in `notifier.py`:

```python
# src/nexus/integration/notifier.py
def verify_slack_request(raw_body: bytes, headers) -> bool:
    secret = SLACK_SIGNING_SECRET
    if not secret:                       # no secret → reject everything
        return False
    ts = headers.get("X-Slack-Request-Timestamp")
    sig = headers.get("X-Slack-Signature")
    if not ts or not sig:
        return False
    if abs(time.time() - int(ts)) > 300:  # 5-min replay guard
        return False
    base = b"v0:" + str(ts).encode() + b":" + raw_body
    expected = "v0=" + hashlib.sha256(base).hexdigest()
    return hmac.compare_digest(expected, str(sig))  # constant-time
```

This implements Slack's documented `v0` signature scheme:
<https://api.slack.com/authentication/verifying-requests-from-slack>.

After parsing, the decision routes to the **same** `HumanApprovalQueue.approve` /
`.reject` the CLI and `/approve`-`/reject` HTTP endpoints use — then, on approve,
re-dispatches through `RunbookExecutor.execute_approved()` so the full governance
plane (cooldown, circuit breaker, OPA allowlist, audit, rollback) still applies.
Governance can still block an action *after* a human approves it (the human may
not have known a heal ran 60s ago or that the breaker is open).

The audit `triggered_by` is recorded as `human:slack:<username>` (the Slack
path passes `username=f"slack:{user}"` into `record_approval`, which prepends
`human:`), making Slack-sourced approvals distinguishable in the audit trail.

---

## 7. End-to-end example

### Approve path
1. A cluster forms an incident with an L3 action (or an LLM proposes a
   remediation). `ActionLadder.evaluate` / the Orchestrator call
   `HumanApprovalQueue.enqueue(...)` → returns `approval_id = "A1B2C3D4"`.
2. `nexus.approvals.required` is published on NATS. `Notifier` consumes it and
   POSTs an interactive message to the Slack channel with two buttons.
3. An operator clicks **Approve ✅**.
4. Slack POSTs to `SLACK_INTERACTIVE_URL` (`/slack/interactive`).
5. `verify_slack_request` passes → payload parsed → `queue.approve("A1B2C3D4")`
   → audit `record_approval("A1B2C3D4", "slack:vatsal")` →
   `executor.execute_approved(pending)` runs the runbook through the ladder.
6. Slack replaces the message with:
   `✅ Approved A1B2C3D4 by @vatsal → success (audit audit-1)`.

### Reject path
1–3 same.
4. Operator clicks **Reject ❌**.
5. `verify_slack_request` passes → `queue.reject("A1B2C3D4")` →
   `record_rejection(...)` → the workflow **terminates**: no `execute_approved`,
   no cluster change.
6. Slack replaces the message with: `❌ Rejected A1B2C3D4 by @vatsal.`

### Triggering it manually without a real incident
```bash
# list staged approvals (any pending ones from prior incidents)
curl -s http://localhost:8080/approvals/pending | jq

# approve via the HTTP equivalent (same queue, same governance path)
curl -s -X POST http://localhost:8080/approve/A1B2C3D4
# reject
curl -s -X POST http://localhost:8080/reject/A1B2C3D4
# CLI equivalents
nexus approve A1B2C3D4
nexus reject  A1B2C3D4
```
These are how to verify the wiring without a Slack round-trip; the Slack handler
calls into the identical logic.

---

## 8. Best practices

- **Always verify the signature.** The ngrok URL is public. Without HMAC
  verification a 3-line curl can take down an L3 resource. Empty
  `SLACK_SIGNING_SECRET` ⟹ 401 on everything by design.
- **Keep the handler under 3s.** Slack retries if it doesn't get 200 in time.
  The current handler is fast; if `execute_approved` ever goes slow, move it to a
  background task and return 200 immediately.
- **`replace_original: true` after a decision.** Prevents double-action by a
  second operator and reflects state. Idempotency (`is_approved`/`is_rejected`)
  is the *code* safety net; the message replace is the *UX* one.
- **Governance still gates after approval.** This is intentional and safe: a
  human approving at T=0 doesn't override a cooldown that started at T=-30s or
  an open circuit breaker (3 consecutive failed post-checks).
- **Don't commit the signing secret.** Load from env / secret manager. The
  `${...}` resolution in `selfheal_config.py` keeps `slack_webhook` out of git
  too.
- **ngrok hygiene.** Use a reserved domain for stable dev; a new free subdomain
  requires updating the Request URL each restart. Don't point prod interactivity
  at a laptop.
- **Audit everything.** Every Slack decision is recorded via
  `record_approval`/`record_rejection` with the `slack:<user>` auditor,
  distinguishable from `api_user` (HTTP) and CLI users.
- **Replay guard.** `verify_slack_request` rejects timestamps >5 min old. If
  you raise `SLACK_REQUEST_TOLERANCE_S`, you widen the replay window.
