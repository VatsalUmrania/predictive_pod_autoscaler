import { cache } from "react"

import type {
  NexusAgentMessage,
  NexusAgentRun,
  NexusAuditRecord,
  NexusDecisionResponse,
  NexusHealth,
  NexusIncident,
  NexusIncidentReport,
  NexusPendingApproval,
  NexusPolicy,
  NexusPrediction,
  NexusPrescalerModeResponse,
  NexusStatus,
} from "./types"

/**
 * Base URL of the NEXUS FastAPI server.
 *
 * Override with `NEXUS_API_BASE_URL`. The default matches the port the backend
 * binds by default (`nexus server start --port`, server_bootstrap.py `status_port`).
 */
const FALLBACK_BASE_URL = "http://127.0.0.1:8080"

export function nexusBaseUrl(): string {
  return (process.env.NEXUS_API_BASE_URL ?? FALLBACK_BASE_URL).replace(/\/+$/, "")
}

/**
 * Operator bearer token for the NEXUS API.
 *
 * The NEXUS operator routes (dashboard, audit, approve/reject) require a
 * bearer token. Every request from this module is made server-side, from a
 * React Server Component or a Server Action, so the token stays on the server
 * and is never shipped to the browser.
 *
 * Do not import this module from a `"use client"` file — that would inline the
 * credential into the browser bundle.
 */
function nexusAuthHeaders(): Record<string, string> {
  const token = process.env.NEXUS_API_TOKEN?.trim()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

export class NexusApiError extends Error {
  readonly status: number
  readonly path: string
  readonly detail?: string

  constructor(status: number, path: string, detail?: string) {
    super(`NEXUS API request failed: ${status} ${path}`)
    this.name = "NexusApiError"
    this.status = status
    this.path = path
    this.detail = detail
  }
}

async function nexusFetch<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${nexusBaseUrl()}${path}`, {
      cache: "no-store",
      ...init,
      headers: {
        ...nexusAuthHeaders(),
        ...(init?.headers as Record<string, string> | undefined),
      },
    })
  } catch (cause) {
    throw new NexusApiError(
      0,
      path,
      cause instanceof Error ? cause.message : "Network request failed"
    )
  }

  if (!response.ok) {
    const detail = await response.text().catch(() => "")
    throw new NexusApiError(response.status, path, detail)
  }

  return (await response.json()) as T
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) search.set(key, String(value))
  }
  const serialized = search.toString()
  return serialized ? `?${serialized}` : ""
}

export const getHealth = cache(async (): Promise<NexusHealth> =>
  nexusFetch<NexusHealth>("/health")
)

export const getStatus = cache(async (): Promise<NexusStatus> =>
  nexusFetch<NexusStatus>("/status")
)

export const getIncidents = cache(
  async (n = 20, app?: string): Promise<NexusIncident[]> =>
    nexusFetch<NexusIncident[]>(
      `/developer/incidents${query({ n, app })}`
    )
)

/**
 * Fetch one incident.
 *
 * Uses the dedicated `GET /developer/incidents/{id}` route, which returns 404
 * with a "No incident found with id '...'" detail when the id is unknown.
 */
export const getIncidentById = cache(
  async (incidentId: string): Promise<NexusIncident | undefined> => {
    try {
      return await nexusFetch<NexusIncident>(
        `/developer/incidents/${encodeURIComponent(incidentId)}`
      )
    } catch (error) {
      if (error instanceof NexusApiError && error.status === 404) {
        return undefined
      }
      throw error
    }
  }
)

export const getPredictions = cache(
  async (app?: string): Promise<NexusPrediction[]> =>
    nexusFetch<NexusPrediction[]>(`/developer/predictions${query({ app })}`)
)

export const getPendingApprovals = cache(async (): Promise<NexusPendingApproval[]> =>
  nexusFetch<NexusPendingApproval[]>("/approvals/pending")
)

export const getAuditTail = cache(
  async (n = 20): Promise<NexusAuditRecord[]> =>
    nexusFetch<NexusAuditRecord[]>(`/audit/tail${query({ n })}`)
)

export const getAuditForIncident = cache(
  async (incidentId: string): Promise<NexusAuditRecord[]> =>
    nexusFetch<NexusAuditRecord[]>(
      `/audit/incident/${encodeURIComponent(incidentId)}`
    )
)

export const getRcaResults = cache(
  async (n = 10): Promise<Array<Record<string, unknown>>> =>
    nexusFetch<Array<Record<string, unknown>>>(`/rca/last${query({ n })}`)
)

export const getAgentRuns = cache(
  async (incidentId: string): Promise<NexusAgentRun[]> =>
    nexusFetch<NexusAgentRun[]>(
      `/developer/incidents/${encodeURIComponent(incidentId)}/agent-runs`
    )
)

export const getAgentMessages = cache(
  async (incidentId: string): Promise<NexusAgentMessage[]> =>
    nexusFetch<NexusAgentMessage[]>(
      `/developer/incidents/${encodeURIComponent(incidentId)}/messages`
    )
)

export const getIncidentReport = cache(
  async (incidentId: string): Promise<NexusIncidentReport> =>
    nexusFetch<NexusIncidentReport>(
      `/developer/incidents/${encodeURIComponent(incidentId)}/report`
    )
)

export const getPolicy = cache(
  async (app: string): Promise<NexusPolicy> =>
    nexusFetch<NexusPolicy>(`/developer/policy/${encodeURIComponent(app)}`)
)

export async function decideIncident(
  incidentId: string,
  approved: boolean,
  reason: string
): Promise<NexusDecisionResponse> {
  return nexusFetch<NexusDecisionResponse>(
    `/developer/incidents/${encodeURIComponent(incidentId)}/${
      approved ? "approve" : "reject"
    }`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        incident_id: incidentId,
        approved,
        approver: "dashboard-operator",
        reason,
      }),
    }
  )
}

export async function setPrescalerMode(
  mode: "shadow" | "advisory" | "autonomous"
): Promise<NexusPrescalerModeResponse> {
  return nexusFetch<NexusPrescalerModeResponse>(`/prescaler/mode/${mode}`, {
    method: "POST",
  })
}
