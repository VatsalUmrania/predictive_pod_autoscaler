import { HugeiconsIcon } from "@hugeicons/react"
import {
  Alert02Icon,
  CheckmarkCircle02Icon,
  Clock01Icon,
  Shield02Icon,
} from "@hugeicons/core-free-icons"

import { cn } from "@/lib/utils"

const ATTENTION_STATUSES = ["AWAITING_APPROVAL", "PENDING_APPROVAL"]
const SUCCESS_STATUSES = ["RESOLVED"]
const FAILURE_STATUSES = [
  "ESCALATED",
  "ROLLED_BACK",
  "EXECUTION_FAILED",
  "POLICY_DENIED",
  "VERIFICATION_FAILED",
  "UNPLANNABLE",
]

type Tone = "attention" | "success" | "failure" | "neutral"

const TONE_CLASS: Record<Tone, string> = {
  attention: "border-chart-2/40 bg-chart-2/10 text-chart-2",
  success: "border-primary/30 bg-primary/10 text-primary",
  failure: "border-destructive/30 bg-destructive/10 text-destructive",
  neutral: "border-border bg-muted text-muted-foreground",
}

function toneFor(status: string, isInterrupted?: boolean): Tone {
  const normalized = status.toUpperCase()
  if (isInterrupted || ATTENTION_STATUSES.includes(normalized)) return "attention"
  if (SUCCESS_STATUSES.includes(normalized)) return "success"
  if (FAILURE_STATUSES.includes(normalized)) return "failure"
  return "neutral"
}

function iconFor(tone: Tone) {
  if (tone === "attention") return Shield02Icon
  if (tone === "success") return CheckmarkCircle02Icon
  if (tone === "failure") return Alert02Icon
  return Clock01Icon
}

/**
 * Status pill for an incident. The color mapping is fixed across every page:
 * blue means it needs an operator, green means it is resolved, red means it failed.
 */
export function StatusBadge({
  status,
  isInterrupted,
  className,
}: {
  status: string
  isInterrupted?: boolean
  className?: string
}) {
  const tone = toneFor(status, isInterrupted)
  const Icon = iconFor(tone)
  const label =
    tone === "attention"
      ? "Awaiting approval"
      : status.replace(/_/g, " ").toLowerCase()

  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded border px-2 py-0.5 font-mono text-[11px] font-semibold",
        TONE_CLASS[tone],
        className
      )}
    >
      <HugeiconsIcon icon={Icon} className="size-3" />
      {label}
    </span>
  )
}

const SEVERITY_TONE: Record<string, Tone> = {
  CRITICAL: "failure",
  ERROR: "failure",
  WARNING: "attention",
  INFO: "neutral",
}

export function SeverityBadge({
  severity,
  className,
}: {
  severity: string
  className?: string
}) {
  const tone = SEVERITY_TONE[severity.toUpperCase()] ?? "neutral"
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center rounded border px-2 py-0.5 font-mono text-[11px] font-semibold",
        TONE_CLASS[tone],
        className
      )}
    >
      {severity.toUpperCase()}
    </span>
  )
}
