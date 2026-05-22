# OpenTUI UX Research Framework

**Document ID:** UX-OPENTUI-001  
**Created:** 2026-05-19  
**Status:** Approved for Implementation  
**Owner:** UX Research Team

---

## Executive Summary

This document provides UX research backing for the OpenTUI terminal operations console. It defines 4 key personas, maps a complete incident response journey across 7 steps with 6 interface screens, and establishes accessibility requirements for WCAG 2.1 AA compliance.

**Key findings:**
- On-call engineers need sub-30-second time-to-first-insight during incidents
- Platform engineers require deep-dive capabilities without leaving the terminal
- Compliance auditors need immutable trails, not just live views
- All users benefit from k9s/htop/gk-style keyboard patterns already familiar to DevOps teams

---

## User Personas

### 1. Sarah — On-Call Engineer

**Role:** Site Reliability Engineer (SRE)  
**Experience:** 5 years in DevOps, 2 years with NEXUS  
**Primary goal:** Detect and resolve incidents within SLA (15 min MTTR)

| Attribute | Details |
|---------|---------|
| **Pain Points** | Too many dashboards, alert fatigue, slow tool cold-start |
| **Tool Proficiency** | Grafana (expert), kubectl (expert), LLM agents (novice) |
| **Accessibility Needs** | Colorblind-safe (deuteranopia) |
| **Success Metric** | "Time from alert to actionable insight" < 30 seconds |

### 2. Marcus — Platform Engineer

**Role:** Infrastructure Developer  
**Experience:** 8 years in Kubernetes, built the NEXUS agents  
**Primary goal:** Optimize agent behavior and runbook effectiveness

| Attribute | Details |
|---------|---------|
| **Pain Points** | No visibility into agent reasoning, hard to debug false positives |
| **Tool Proficiency** | Prometheus (expert), NATS (expert), LLM prompting (intermediate) |
| **Accessibility Needs** | High contrast mode, compact layouts for small screens |
| **Success Metric** | "False positive rate" and "% runbooks with L2+ confidence" |

### 3. Jennifer — Compliance Auditor

**Role:** Risk & Compliance Officer  
**Experience:** 10 years in financial services compliance  
**Primary goal:** Verify governance controls and audit trails

| Attribute | Details |
|---------|---------|
| **Pain Points** | Immutable logs are queryable but not visual, manual evidence gathering |
| **Tool Proficiency** | SQL (intermediate), Jira (expert), CLI tools (novice) |
| **Accessibility Needs** | Screen reader compatibility, high contrast |
| **Success Metric** | "Evidence gathering time per audit" < 1 hour |

### 4. David — Service Owner

**Role:** Application Team Lead  
**Experience:** 6 years managing microservices  
**Primary goal:** Understand how NEXUS decisions impact app SLOs

| Attribute | Details |
|---------|---------|
| **Pain Points** | No visibility into why agent took action X, manual correlation |
| **Tool Proficiency** | K8s (intermediate), Grafana (intermediate), LLM agents (curious) |
| **Accessibility Needs** | None specific |
| **Success Metric** | "% of scaling events understood vs mysterious" |

---

## Journey Map: "Respond to OOMKill Incident"

**Scenario:** Production pod crash due to memory leak, NEXUS must detect, diagnose, and heal.

### Step 1: Alert Fires (T+0s)

**Trigger:** Prometheus detects 3x OOMKill events in 5 minutes  
**Sarah's State:** Paged via PagerDuty, opens laptop  
**Emotional State:** 😟 Anxious — "Is this a real incident or noise?"

**Information Needed:**
- What service is affected?
- How many replicas impacted?
- Has this happened before?

**UX Pattern:** Notification banner with severity color, 1-line root cause preview

### Step 2: Launch OpenTUI (T+10s)

**Action:** Sarah runs `nexus-tui` in terminal  
**System Load:** Cold starts TUI app, connects to trace backend  
**Emotional State:** 😐 Neutral — waiting for UI to render

**Success Criteria:**
- Cold start < 2 seconds (tracked via performance.now())
- Auto-reconnect if backend unavailable

**UX Pattern:** Loading skeleton with spinner, last-known-good state cached

### Step 3: Dashboard View (T+15s)

**Screen:** Live agent dashboard shows all 7 domain agents  
**Sarah's Goal:** Identify which agent detected the OOMKill

**Dashboard Layout:**
```
┌──────────────────────────────────────────────────────────────┐
│ PPA + NEXUS Operations [LIVE] 🔴                           │
├──────────────┬──────────────────────┬───────────────────────┤
│ PPA Scaling  │ NEXUS Agents         │ Cloud Events          │
│ ───────────  │ ──────────────────── │ ───────────────────── │
│ Pods: 4→7    │ 🟢 K8s Agent         │ AWS: EC2 OK           │
│ Prediction:  │ 🟢 NGINX Agent       │ GCP: GKE OK           │
│ +37%         │ 🔴 RCA: OOMKill L2   │ Prometheus: 3 alerts  │
│              │ 🟡 Metrics (watch)   │                       │
└──────────────┴──────────────────────┴───────────────────────┘
```

**Emotional State:** 😊 Confident — immediate situational awareness

### Step 4: Deep Dive — Agent Detail Panel (T+30s)

**Screen:** Side panel slides out with agent details  
**Sarah's Goal:** Understand what the agent is seeing

**Agent Detail Panel:**
```
┌──────────────────────────────────────────────────────────────┐
│ Agent-3 (K8s Domain) — ACTIVE                                │
├──────────────────────────────────────────────────────────────┤
│ State: ANALYZING → Confidence: 87% → Runbook: RESTART_POD   │
│                                                              │
│ Latest Prompt (streaming):                                   │
│ "Analyzing OOMKill on pod-7ab2c: checking memory leak        │
│ pattern from last 3 restarts. Comparing with 2026-05-18      │
│ incident #3421 (same failure class: memory_leak)..."         │
│                                                              │
│ Cloud Correlation:                                           │
│ - GCP: Memory utilization 94% (↑12%)                         │
│ - Prometheus: container_memory_usage_bytes > threshold       │
│ - NATS: 0 pending events (queue healthy)                     │
└──────────────────────────────────────────────────────────────┘
```

**Emotional State:** 🧠 Understanding — "The agent is comparing to historical incidents"

### Step 5: Action Confirmation (T+2m)

**Screen:** ActionLadder shows proposed L2 action  
**Sarah's Decision:** Approve or override agent's recommended action

**Action Confirmation Panel:**
```
┌──────────────────────────────────────────────────────────────┐
│ Action Approval Required (L2)                                │
├──────────────────────────────────────────────────────────────┤
│ Agent: K8s-3 → pod-7ab2c (namespace: production)             │
│                                                              │
│ Runbook: RESTART_POD                                         │
│ Confidence: 87% (based on 14 historical matches)             │
│                                                              │
│ Action: kubectl delete pod pod-7ab2c -n production           │
│                                                              │
│ Countdown: [00:23] — Auto-approve on confidence > 90%        │
│                                                              │
│ [a] Approve  [r] Reject  [e] Edit Action                     │
└──────────────────────────────────────────────────────────────┘
```

**Emotional State:** ⚖️ Deciding — "87% confidence, but I want to review the runbook"

### Step 6: Execution Progress (T+3m)

**Screen:** Live feedback during action execution  
**Sarah's Goal:** Confirm action completed successfully

**Execution Panel:**
```
┌──────────────────────────────────────────────────────────────┐
│ Executing: RESTART_POD                                       │
├──────────────────────────────────────────────────────────────┤
│ [✓] Step 1: Drain pod-7ab2c                                 │
│ [✓] Step 2: Delete pod                                       │
│ [→] Step 3: Wait for new pod to start (32s elapsed)          │
│ [ ] Step 4: Verify health check                              │
│                                                              │
│ Live Logs:                                                   │
│ pod-7ab2c is being deleted                                   │
│ pod-7ab2c deleted                                            │
│ pod-7ab2c-2 created                                          │
│ pod-7ab2c-2 is now Ready                                     │
└──────────────────────────────────────────────────────────────┘
```

**Emotional State:** 😌 Satisfied — action progressing as expected

### Step 7: Resolution Summary (T+5m)

**Screen:** Post-action summary card  
**System Action:** Logs incident to audit DB, updates metrics

**Resolution Panel:**
```
┌──────────────────────────────────────────────────────────────┐
│ ✅ Incident Resolved                                         │
├──────────────────────────────────────────────────────────────┤
│ Timeline:                                                    │
│ - T+0: OOMKill detected (3 containers)                       │
│ - T+15s: Agent started analysis                              │
│ - T+2m: Action approved (87% confidence)                     │
│ - T+3m: Pod restarted successfully                           │
│                                                              │
│ Audit Reference: #INC-2026-05-19-3847                        │
│ SHA256: a1b2c3d4e5f6...                                      │
│                                                              │
│ [q] Return to Dashboard  [c] Copy Summary                    │
└──────────────────────────────────────────────────────────────┘
```

**Emotional State:** ✅ Accomplished — incident resolved within SLA

---

## Screen Mockups (ASCII Wireframes)

### Screen 1: Dashboard (Main View)

```
┌────────────────────────────────────────────────────────────────────────┐
│ PPA + NEXUS Operations [LIVE]                              14:32:05   │
├──────────────────────┬───────────────────────────┬─────────────────────┤
│ PPA Scaling          │ NEXUS Agents              │ Cloud Events        │
│ ───────────────────  │ ───────────────────────── │ ─────────────────── │
│ Replicas: 4 → 7      │ 🟢 K8s Agent              │ AWS: EC2 OK         │
│ Prediction: +37%     │ 🟢 NGINX Agent            │ GCP: GKE OK         │
│ ETA: 2min            │ 🔴 RCA: OOMKill L2        │ Prometheus: 3 alerts│
│ Circuit: CLOSED      │ 🟡 Metrics (watch)        │ NATS: 0 pending     │
│                      │ 🟢 Git Agent              │                     │
│ Historical:          │ 🟢 DB Agent               │                     │
│ 14:30: +2 replicas   │ 🟢 Network Agent          │ [1] View Detail     │
│ 14:00: stable        │ 🟢 Config Agent           │                     │
└──────────────────────┴───────────────────────────┴─────────────────────┘
│ Query > _                                                    [F1 Search]│
└────────────────────────────────────────────────────────────────────────┘
```

### Screen 2: Agent Detail Panel (Side View)

```
┌────────────────────────────────────────────────────────────────────────┐
│ PPA Scaling          │ Agent-3 Detail                    │ Cloud Events│
│                      │ ─────────────────────────────────────────────── │
│ [unchanged]          │ Agent: K8s-3 (active)                           │
│                      │ State: ANALYZING                                │
│                      │ Confidence: 87%                                 │
│                      │ Runbook: RESTART_POD                            │
│                      │                                                 │
│                      │ Live Prompt (streaming):                        │
│                      │ "Analyzing OOMKill on pod-7ab2c: checking       │
│                      │ memory leak pattern from last 3 restarts.       │
│                      │ Comparing with incident #3421 (2026-05-18)..."  │
│                      │                                                 │
│                      │ Cloud Correlation:                              │
│                      │ - GCP: memory 94% (↑12%)                        │
│                      │ - Prometheus: threshold breach                  │
│                      │ - NATS: queue healthy (0 pending)               │
│                      │                                                 │
│                      └─────────────────────────────────────────────────┤
│ Query > "show pod-7ab2c events"                          [F1 Search]  │
└────────────────────────────────────────────────────────────────────────┘
```

### Screen 3: Query Results

```
┌────────────────────────────────────────────────────────────────────────┐
│ PPA + NEXUS Operations                                     14:32:10   │
├────────────────────────────────────────────────────────────────────────┤
│ Query Results: "show pod-7ab2c events"                                   │
├────────────────────────────────────────────────────────────────────────┤
│ Time     │ Agent   │ Event                    │ Confidence │ Action   │
│ ──────── │ ─────── │ ──────────────────────── │ ────────── │ ─────── │
│ 14:28:03 │ K8s-3   │ OOMKill detected         │ 94%        │ L2      │
│ 14:28:45 │ K8s-3   │ Analysis started         │ -          │ -       │
│ 14:29:12 │ K8s-3   │ RCA: memory_leak         │ 87%        │ L2      │
│ 14:30:01 │ K8s-3   │ Action: RESTART_POD      │ 87%        │ pending │
│                                                              │         │
│ [Tab] Navigate  [Enter] Select  [c] Copy  [Esc] Close        │
└────────────────────────────────────────────────────────────────────────┘
```

### Screen 4: Action Confirmation Modal

```
┌────────────────────────────────────────────────────────────────────────┐
│                                                                        │
│                      ✅ Action Approval Required (L2)                  │
│                                                                        │
│   Agent:     K8s-3 → pod-7ab2c (namespace: production)                 │
│   Runbook:   RESTART_POD                                               │
│   Confidence:                                                           │
│              ███████████████████████████░░░░░░░  87%                    │
│              (based on 14 historical matches)                          │
│                                                                        │
│   Action:    kubectl delete pod pod-7ab2c -n production                │
│                                                                        │
│   ┌──────────────────────────────────────────────────────────────────┐ │
│   │ Auto-approve in: [00:23]                                         │ │
│   │ (Auto-approve fires on confidence > 90%)                         │ │
│   └──────────────────────────────────────────────────────────────────┘ │
│                                                                        │
│          ┌───────┐      ┌────────────┐      ┌──────────┐               │
│          │ Approve│      │   Reject   │      │ Edit    │              │
│          │  (a)  │      │    (r)     │      │  (e)    │              │
│          └───────┘      └────────────┘      └──────────┘               │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

### Screen 5: Execution Progress

```
┌────────────────────────────────────────────────────────────────────────┐
│ Executing: RESTART_POD                                                 │
├────────────────────────────────────────────────────────────────────────┤
│ Steps:                                                                 │
│                                                                        │
│ [✓] Step 1: Drain pod-7ab2c (completed 14:32:15)                       │
│ [✓] Step 2: Delete pod (completed 14:32:18)                            │
│ [→] Step 3: Wait for new pod to start (32s elapsed)                    │
│ [ ] Step 4: Verify health check                                        │
│                                                                        │
│ Live Logs:                                                             │
│ ─────────────────────────────────────────────────────────────────────  │
│ 14:32:15  pod-7ab2c is being deleted                                   │
│ 14:32:18  pod-7ab2c deleted                                            │
│ 14:32:20  pod-7ab2c-2 created                                          │
│ 14:32:45  pod-7ab2c-2 is now Ready                                     │
│                                                                        │
│ [Esc] Cancel Execution                                                 │
└────────────────────────────────────────────────────────────────────────┘
```

### Screen 6: Resolution Summary

```
┌────────────────────────────────────────────────────────────────────────┐
│ ✅ Incident Resolved                                                  │
├────────────────────────────────────────────────────────────────────────┤
│ Timeline:                                                              │
│ ─────────────────────────────────────────────────────────────────────  │
│ T+0       OOMKill detected (3 containers)                              │
│ T+15s     Agent started analysis                                       │
│ T+2m      Action approved (87% confidence)                             │
│ T+3m      Pod restarted successfully                                   │
│                                                                        │
│ Audit Reference: #INC-2026-05-19-3847                                  │
│ SHA256: a1b2c3d4e5f67890abcdef...                                      │
│                                                                        │
│ Metrics Updated:                                                       │
│ - MTTR: 3m 12s (vs 15m SLA)                                           │
│ - Confidence Score: 87% (above 80% threshold)                          │
│ - Runbook Effectiveness: 94% (14/15 successful)                        │
│                                                                        │
│          ┌────────────┐              ┌────────────┐                    │
│          │ Dashboard  │              │  Copy JSON │                    │
│          │    (q)     │              │    (c)     │                    │
│          └────────────┘              └────────────┘                    │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Accessibility Requirements

### Color Palette (Colorblind-Safe)

| Role | Color | Hex | Usage |
|------|-------|-----|-------|
| Critical | Red | `#D73027` | OOMKill, errors, L0 actions |
| Warning | Orange | `#F46D43` | Resource exhaustion, L1 actions |
| Info | Blue | `#4575B4` | Normal operations, L2 actions |
| Success | Green | `#1A9850` | Resolved, approved actions |
| Text | White | `#F8F8F2` | Primary text on dark bg |
| Background | Dark Grey | `#1E1E2E` | Terminal background |
| Muted | Grey | `#6C6C7C` | Secondary text |

### Keyboard Navigation

| Key | Action | Context |
|-----|--------|---------|
| `Tab` / `Shift+Tab` | Navigate between panels | All screens |
| `1`–`7` | Jump to panel N | Dashboard |
| `Enter` | Select / Open detail | All screens |
| `Esc` | Go back / Close panel | All screens |
| `r` | Refresh data | All screens |
| `/` | Focus query input | All screens |
| `c` | Copy to clipboard | Detail views |
| `a` | Approve action | Confirmation modal |
| `r` | Reject action | Confirmation modal |

### Screen Reader Compatibility

- ARIA labels on all interactive elements
- Status updates announced via live regions
- Semantic ordering matches visual layout

### WCAG 2.1 AA Compliance

- Text contrast ratio: 4.5:1 minimum
- Focus indicators: 3px outline minimum
- No information conveyed by color alone

---

## Design Principles

### 1. Terminal-First Aesthetics (k9s, htop, gh)

**Why:** Target users live in terminals; muscle memory should transfer  
**How to apply:** Use 80/24 grid, monospace fonts, ASCII borders, no mouse dependency

### 2. Information Density Control (Grafana + htop hybrid)

**Why:** Dashboards overwhelm; terminals overwhelm with detail  
**How to apply:** Default view shows 5 Ws (what, where, when, confidence, action), expand on demand

### 3. Streaming > Polling (Vercel CLI, gh CLI)

**Why:** Polling feels laggy; streaming feels instant  
**How to apply:** Server-sent events or long-poll for live prompt tokens, badge updates

### 4. Confidence Transparency (NEXUS native)

**Why:** Users must know when to trust agent vs. override  
**How to apply:** Show numeric % + visual bar + historical match count for every action

### 5. Audit Trail Visibility (Compliance requirement)

**Why:** Financial/compliance users need immutable references  
**How to apply:** Every screen shows SHA256 hash + incident ID, copy-to-clipboard

### 6. Keyboard-Only Workflow (k9s influence)

**Why:** Mouse breaks flow during incidents  
**How to apply:** Every action accessible via keybinding, Tab navigation, `g` + `r` hotkeys

### 7. Cold-Start Awareness (performance budget)

**Why:** Slow tools frustrate users in emergencies  
**How to apply:** < 2s cold start (Bun), < 500ms query response, cached last-known state

---

## Validation Checklist

Before implementation begins, validate:

- [ ] 4 personas reviewed and approved by real users matching profiles
- [ ] Journey map tested with 5+ on-call engineers via scenario walkthrough
- [ ] Color palette tested with deuteranopia, protanopia, tritanopia simulators
- [ ] Keyboard navigation tested without mouse for all 6 screens
- [ ] Screen reader compatibility tested (VoiceOver, NVDA)
- [ ] Performance metrics verified on cold start (<2s)
- [ ] Accessibility audit passed (automated + manual testing)

---

*Generated: 2026-05-19*
