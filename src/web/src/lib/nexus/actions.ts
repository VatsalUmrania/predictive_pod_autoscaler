"use server"

import { revalidatePath } from "next/cache"

import { decideIncident, setPrescalerMode } from "./client"
import { NexusApiError } from "./client"
import type {
  NexusDecisionResponse,
  NexusPrescalerModeResponse,
} from "./types"

export type NexusActionError = {
  message: string
  status: number
  path: string
}

export type ActionResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: NexusActionError }

function toActionError(error: unknown): NexusActionError {
  if (error instanceof NexusApiError) {
    if (error.status === 0) {
      return {
        message: "The NEXUS engine is unreachable.",
        status: error.status,
        path: error.path,
      }
    }
    if (error.status === 401) {
      return {
        message:
          "The NEXUS engine rejected the operator token. Set NEXUS_API_TOKEN in the web app's environment to match the server's NEXUS_ADMIN_TOKEN.",
        status: error.status,
        path: error.path,
      }
    }
    if (error.status === 503 && error.detail?.includes("authentication")) {
      return {
        message:
          "The NEXUS engine has no operator token configured. Set NEXUS_ADMIN_TOKEN on the server.",
        status: error.status,
        path: error.path,
      }
    }
    return {
      message: `The NEXUS engine returned HTTP ${error.status}.`,
      status: error.status,
      path: error.path,
    }
  }
  return {
    message:
      error instanceof Error ? error.message : "An unexpected error occurred.",
    status: 0,
    path: "",
  }
}

/**
 * Approve or reject an incident that is paused at the approval node.
 *
 * The action returns the engine's real response. Nothing about the outcome is
 * assumed or optimistically fabricated on the client.
 */
export async function decideIncidentAction(
  incidentId: string,
  approved: boolean,
  reason: string
): Promise<ActionResult<NexusDecisionResponse>> {
  try {
    const data = await decideIncident(incidentId, approved, reason)
    revalidatePath("/")
    revalidatePath("/approvals")
    revalidatePath("/incidents")
    revalidatePath(`/incidents/${incidentId}`)
    return { ok: true, data }
  } catch (error) {
    return { ok: false, error: toActionError(error) }
  }
}

/** Change the predictive autoscaler autonomy mode. */
export async function setPrescalerModeAction(
  mode: "shadow" | "advisory" | "autonomous"
): Promise<ActionResult<NexusPrescalerModeResponse>> {
  try {
    const data = await setPrescalerMode(mode)
    revalidatePath("/")
    revalidatePath("/predictions")
    return { ok: true, data }
  } catch (error) {
    return { ok: false, error: toActionError(error) }
  }
}
