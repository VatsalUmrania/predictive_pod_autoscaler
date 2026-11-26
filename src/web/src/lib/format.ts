/**
 * Deterministic formatters for operational data.
 *
 * Everything here renders in UTC with a fixed format so a server-rendered value and
 * the client hydration produce identical strings. Relative timestamps are avoided
 * because "3 minutes ago" differs between the server render and the browser.
 */

/**
 * Parse a timestamp into a Date.
 *
 * Legacy dashboard rows were written as `"2026-11-25 10:00:00"` — UTC, but
 * with a space and no offset. `new Date()` parses that shape as *local* time,
 * which silently shifts every timestamp by the viewer's UTC offset. Detect the
 * shape and pin it to UTC before parsing.
 */
function parseUtc(value: string): Date {
  const legacy = /^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})$/.exec(value)
  if (legacy) return new Date(`${legacy[1]}T${legacy[2]}Z`)
  return new Date(value)
}

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "unknown"
  const parsed = parseUtc(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toISOString().replace("T", " ").slice(0, 19) + "Z"
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "unknown"
  const parsed = parseUtc(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toISOString().slice(0, 10)
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-"
  return `${Math.round(value * 100)}%`
}

export function formatNumber(
  value: number | null | undefined,
  fractionDigits = 0
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-"
  return value.toLocaleString("en-US", {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  })
}

export function formatDurationMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "-"
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

export function formatUptime(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
    return "-"
  }
  const total = Math.floor(seconds)
  const days = Math.floor(total / 86400)
  const hours = Math.floor((total % 86400) / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}m`
  return `${minutes}m`
}
