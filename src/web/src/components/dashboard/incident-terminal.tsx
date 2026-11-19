"use client"

import * as React from "react"
import { Incident } from "@/types/incident"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  CommandLineIcon,
  Copy01Icon,
  Tick02Icon,
  Clock01Icon,
  CheckmarkCircle02Icon,
  Alert02Icon,
  ArrowTurnBackwardIcon,
  FileCodeIcon,
} from "@hugeicons/core-free-icons"
import {
  Trace,
  TraceLine,
  TraceTitle,
  TraceDescription,
  TraceCollapsible,
  TraceCollapsibleTrigger,
  TraceCollapsibleContent,
} from "@/components/ui/trace"

interface IncidentTerminalProps {
  incident: Incident
  className?: string
}

export function IncidentTerminal({ incident, className }: IncidentTerminalProps) {
  const [activeTab, setActiveTab] = React.useState<"execution" | "evidence" | "rollback">("execution")
  const [copied, setCopied] = React.useState(false)

  const executionOutput = incident.execution.results
    .map(
      (r, i) =>
        `[STEP ${r.step ?? i + 1}] ${r.tool_name} (duration: ${
          r.duration_ms ?? "N/A"
        }ms)\nSTATUS: ${r.status}\nOUTPUT:\n${r.output}\n`
    )
    .join("\n--------------------------------------------------\n\n")

  const evidenceOutput = incident.evidence
    .map((e, i) => `[EVIDENCE #${i + 1}]\n${typeof e === "string" ? e : JSON.stringify(e, null, 2)}`)
    .join("\n\n")

  const rollbackOutput = incident.rollback.results
    .map((r, i) => `[ROLLBACK #${i + 1}] ${r.tool_name}\nSTATUS: ${r.status}\nOUTPUT:\n${r.output}`)
    .join("\n\n")

  const currentContent =
    activeTab === "execution"
      ? executionOutput || "// No execution log output available yet."
      : activeTab === "evidence"
      ? evidenceOutput || "// No raw evidence streams found."
      : rollbackOutput || "// No rollback executed for this thread."

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(currentContent)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // fallback
    }
  }

  return (
    <div className={cn("overflow-hidden rounded-lg border border-border bg-card", className)}>
      {/* Terminal Top Bar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-muted/40 px-3 py-2 text-xs">
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1.5" aria-hidden="true">
            <span className="size-2.5 rounded-full bg-destructive/70" />
            <span className="size-2.5 rounded-full bg-chart-2/70" />
            <span className="size-2.5 rounded-full bg-primary/70" />
          </div>
          <div className="flex items-center gap-1.5 pl-2 font-mono text-xs font-semibold text-foreground">
            <HugeiconsIcon icon={CommandLineIcon} className="size-3.5 text-primary" />
            <span>NEXUS-REACTIVE-TERMINAL</span>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {/* Tab buttons */}
          <div className="flex items-center rounded-md border border-border bg-background p-0.5 text-xs">
            <button
              type="button"
              onClick={() => setActiveTab("execution")}
              className={cn(
                "rounded px-2.5 py-1 font-mono transition-colors",
                activeTab === "execution"
                  ? "bg-muted font-semibold text-foreground shadow-xs"
                  : "text-muted-foreground hover:text-foreground"
              )}
            >
              Execution ({incident.execution.results.length})
            </button>
            <button
              type="button"
              onClick={() => setActiveTab("evidence")}
              className={cn(
                "rounded px-2.5 py-1 font-mono transition-colors",
                activeTab === "evidence"
                  ? "bg-muted font-semibold text-foreground shadow-xs"
                  : "text-muted-foreground hover:text-foreground"
              )}
            >
              Evidence ({incident.evidence.length})
            </button>
            <button
              type="button"
              onClick={() => setActiveTab("rollback")}
              className={cn(
                "rounded px-2.5 py-1 font-mono transition-colors",
                activeTab === "rollback"
                  ? "bg-muted font-semibold text-foreground shadow-xs"
                  : "text-muted-foreground hover:text-foreground"
              )}
            >
              Rollback ({incident.rollback.results.length})
            </button>
          </div>

          <Button
            variant="outline"
            size="sm"
            onClick={handleCopy}
            className="h-7 text-xs font-mono gap-1.5"
          >
            <HugeiconsIcon
              icon={copied ? Tick02Icon : Copy01Icon}
              className={cn("size-3.5", copied && "text-primary")}
            />
            <span>{copied ? "Copied" : "Copy"}</span>
          </Button>
        </div>
      </div>

      {/* Terminal Body with Trace */}
      <div className="relative max-h-96 w-full overflow-y-auto bg-muted/20 p-4 font-mono text-xs leading-relaxed text-foreground select-text">
        {/* Using Trace for the stream items */}
        <Trace className="space-y-2">
          {activeTab === "execution" && (
            incident.execution.results.length === 0 ? (
              <TraceLine className="text-muted-foreground">
                {"// No execution log output available yet."}
              </TraceLine>
            ) : (
              incident.execution.results.map((res, idx) => {
                const isSuccess = res.status === "SUCCESS"
                return (
                  <TraceCollapsible key={idx} defaultOpen={idx === 0 || !isSuccess}>
                    <TraceCollapsibleTrigger className="w-full justify-between rounded border border-border/60 bg-background/60 p-2 hover:bg-background">
                      <TraceLine status={isSuccess ? undefined : "error"}>
                        <HugeiconsIcon
                          icon={isSuccess ? CheckmarkCircle02Icon : Alert02Icon}
                          className={cn("size-3.5 shrink-0", isSuccess ? "text-primary" : "text-destructive")}
                        />
                        <TraceTitle className="font-mono font-semibold text-foreground">
                          Step {res.step ?? idx + 1}: {res.tool_name}
                        </TraceTitle>
                      </TraceLine>
                      <div className="flex items-center gap-2">
                        {res.duration_ms !== undefined && (
                          <TraceDescription className="flex items-center gap-1 font-mono text-[11px]">
                            <HugeiconsIcon icon={Clock01Icon} className="size-3" />
                            {res.duration_ms}ms
                          </TraceDescription>
                        )}
                        <span
                          className={cn(
                            "rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold",
                            isSuccess
                              ? "border border-primary/30 bg-primary/10 text-primary"
                              : "border border-destructive/30 bg-destructive/10 text-destructive"
                          )}
                        >
                          {res.status}
                        </span>
                      </div>
                    </TraceCollapsibleTrigger>
                    <TraceCollapsibleContent className="border-l-primary/40">
                      <pre className="overflow-x-auto rounded border border-border bg-background p-2.5 font-mono text-xs whitespace-pre-wrap text-foreground">
                        {res.output}
                      </pre>
                    </TraceCollapsibleContent>
                  </TraceCollapsible>
                )
              })
            )
          )}

          {activeTab === "evidence" && (
            incident.evidence.length === 0 ? (
              <TraceLine className="text-muted-foreground">
                {"// No raw evidence streams found."}
              </TraceLine>
            ) : (
              incident.evidence.map((ev, idx) => {
                const textContent = typeof ev === "string" ? ev : JSON.stringify(ev, null, 2)
                return (
                  <TraceCollapsible key={idx} defaultOpen={idx === 0}>
                    <TraceCollapsibleTrigger className="w-full justify-between rounded border border-border/60 bg-background/60 p-2 hover:bg-background">
                      <TraceLine>
                        <HugeiconsIcon icon={FileCodeIcon} className="size-3.5 text-chart-2 shrink-0" />
                        <TraceTitle className="font-mono text-foreground">
                          Evidence Stream #{idx + 1}
                        </TraceTitle>
                      </TraceLine>
                      <TraceDescription className="font-mono text-[11px]">
                        {textContent.split("\n").length} lines
                      </TraceDescription>
                    </TraceCollapsibleTrigger>
                    <TraceCollapsibleContent className="border-l-chart-2/40">
                      <pre className="overflow-x-auto rounded border border-border bg-background p-2.5 font-mono text-xs whitespace-pre-wrap text-foreground">
                        {textContent}
                      </pre>
                    </TraceCollapsibleContent>
                  </TraceCollapsible>
                )
              })
            )
          )}

          {activeTab === "rollback" && (
            incident.rollback.results.length === 0 ? (
              <TraceLine className="text-muted-foreground">
                {"// No rollback executed for this thread."}
              </TraceLine>
            ) : (
              incident.rollback.results.map((rb, idx) => (
                <TraceCollapsible key={idx} defaultOpen>
                  <TraceCollapsibleTrigger className="w-full justify-between rounded border border-destructive/30 bg-destructive/10 p-2 hover:bg-destructive/15">
                    <TraceLine status="error">
                      <HugeiconsIcon icon={ArrowTurnBackwardIcon} className="size-3.5 text-destructive shrink-0" />
                      <TraceTitle className="font-mono font-semibold text-destructive">
                        Rollback #{idx + 1}: {rb.tool_name}
                      </TraceTitle>
                    </TraceLine>
                    <span className="rounded border border-destructive/30 bg-destructive/20 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-destructive">
                      {rb.status}
                    </span>
                  </TraceCollapsibleTrigger>
                  <TraceCollapsibleContent className="border-l-destructive/40">
                    <pre className="overflow-x-auto rounded border border-border bg-background p-2.5 font-mono text-xs whitespace-pre-wrap text-foreground">
                      {rb.output}
                    </pre>
                  </TraceCollapsibleContent>
                </TraceCollapsible>
              ))
            )
          )}
        </Trace>
      </div>
    </div>
  )
}


