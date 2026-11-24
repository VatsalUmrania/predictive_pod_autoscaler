import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { EmptyState } from "@/components/empty-state"
import { formatDurationMs, formatNumber, formatTimestamp } from "@/lib/format"
import type { NexusAgentMessage, NexusAgentRun } from "@/lib/nexus/types"
import { cn } from "@/lib/utils"

const STATUS_CLASS: Record<string, string> = {
  completed: "border-primary/30 bg-primary/10 text-primary",
  success: "border-primary/30 bg-primary/10 text-primary",
  running: "border-chart-2/30 bg-chart-2/10 text-chart-2",
  pending: "border-border bg-muted text-muted-foreground",
  failed: "border-destructive/30 bg-destructive/10 text-destructive",
  error: "border-destructive/30 bg-destructive/10 text-destructive",
}

function pill(value: string) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center rounded border px-2 py-0.5 font-mono text-[11px] font-semibold",
        STATUS_CLASS[value.toLowerCase()] ?? STATUS_CLASS.pending
      )}
    >
      {value.toLowerCase()}
    </span>
  )
}

/**
 * Agent runs and reasoning messages for one incident.
 *
 * The token counts, durations and model names come from `agent_runs` and
 * `agent_messages` in the NEXUS database.
 */
export function AgentActivity({
  runs,
  messages,
  className,
}: {
  runs: NexusAgentRun[]
  messages: NexusAgentMessage[]
  className?: string
}) {
  if (runs.length === 0 && messages.length === 0) {
    return (
      <EmptyState
        title="No agent activity recorded"
        description="The NEXUS engine has not stored any agent runs or messages for this incident."
        className={className}
      />
    )
  }

  const totalTokens = runs.reduce((sum, run) => sum + (run.total_tokens || 0), 0)

  return (
    <div className={cn("space-y-4", className)}>
      {runs.length > 0 ? (
        <div className="overflow-x-auto rounded-lg border border-border">
          <Table className="w-full min-w-[820px]">
            <TableHeader>
              <TableRow>
                <TableHead className="font-mono text-xs">Node</TableHead>
                <TableHead className="font-mono text-xs">Agent</TableHead>
                <TableHead className="font-mono text-xs">Model</TableHead>
                <TableHead className="font-mono text-xs">Status</TableHead>
                <TableHead className="text-right font-mono text-xs">Prompt</TableHead>
                <TableHead className="text-right font-mono text-xs">Completion</TableHead>
                <TableHead className="text-right font-mono text-xs">Total</TableHead>
                <TableHead className="text-right font-mono text-xs">Duration</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((run) => (
                <TableRow key={run.run_id}>
                  <TableCell className="font-mono text-xs">
                    {run.node_name ?? "-"}
                  </TableCell>
                  <TableCell className="font-mono text-xs">{run.agent_type}</TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {run.model_name}
                  </TableCell>
                  <TableCell>{pill(run.status)}</TableCell>
                  <TableCell className="text-right font-mono text-xs tabular-nums">
                    {formatNumber(run.prompt_tokens)}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs tabular-nums">
                    {formatNumber(run.completion_tokens)}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs font-semibold tabular-nums">
                    {formatNumber(run.total_tokens)}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs tabular-nums text-muted-foreground">
                    {formatDurationMs(run.duration_ms)}
                  </TableCell>
                </TableRow>
              ))}
              <TableRow className="bg-muted/30">
                <TableCell
                  colSpan={6}
                  className="font-mono text-xs font-semibold"
                >
                  Total tokens across {runs.length} run{runs.length === 1 ? "" : "s"}
                </TableCell>
                <TableCell className="text-right font-mono text-xs font-bold tabular-nums">
                  {formatNumber(totalTokens)}
                </TableCell>
                <TableCell />
              </TableRow>
            </TableBody>
          </Table>
        </div>
      ) : null}

      {runs.some((run) => run.error_message) ? (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">
          {runs
            .filter((run) => run.error_message)
            .map((run) => (
              <p key={run.run_id} className="font-mono">
                {run.node_name ?? run.agent_type}: {run.error_message}
              </p>
            ))}
        </div>
      ) : null}

      {messages.length > 0 ? (
        <div className="space-y-2">
          <h3 className="font-mono text-xs font-semibold text-muted-foreground">
            Reasoning trace
          </h3>
          {messages.map((message) => (
            <article
              key={message.message_id}
              className="rounded-lg border border-border bg-card p-3"
            >
              <div className="flex flex-wrap items-center gap-2 font-mono text-[11px] text-muted-foreground">
                <span className="rounded border border-border bg-muted px-1.5 py-0.5 font-semibold text-foreground">
                  {message.role}
                </span>
                <span>#{message.sequence_num}</span>
                <span>{formatTimestamp(message.created_at)}</span>
              </div>
              {message.content ? (
                <p className="mt-2 whitespace-pre-wrap font-mono text-xs leading-relaxed">
                  {message.content}
                </p>
              ) : null}
              {message.reasoning_content ? (
                <p className="mt-2 whitespace-pre-wrap border-l-2 border-border pl-3 text-xs leading-relaxed text-muted-foreground">
                  {message.reasoning_content}
                </p>
              ) : null}
            </article>
          ))}
        </div>
      ) : null}
    </div>
  )
}
