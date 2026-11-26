import { NexusApiError } from "./client"

export type ApiErrorInfo = {
  message: string
  status?: number
  path?: string
}

/**
 * Turns a thrown API error into something an operator can act on.
 *
 * The status and the failing path are shown so the operator knows exactly which
 * endpoint is not answering.
 */
export function describeApiError(error: unknown): ApiErrorInfo {
  if (error instanceof NexusApiError) {
    if (error.status === 0) {
      return {
        message: "The NEXUS engine did not respond.",
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
  }
}
