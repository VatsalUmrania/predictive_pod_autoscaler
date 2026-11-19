"use client"

import * as React from "react"
import { Incident, PredictionDecision } from "@/types/incident"
import {
  SAMPLE_INCIDENTS,
  SAMPLE_PREDICTIONS,
  normalizeIncident,
} from "@/lib/sample-data"

interface IncidentContextType {
  incidents: Incident[]
  predictions: PredictionDecision[]
  isLoading: boolean
  isOnline: boolean
  isDemoMode: boolean
  lastSync: Date | null
  toggleDemoMode: (enable: boolean) => void
  refreshData: () => Promise<void>
  approveIncident: (incidentId: string, reason?: string) => Promise<boolean>
  rejectIncident: (incidentId: string, reason?: string) => Promise<boolean>
  getIncident: (incidentId: string) => Incident | undefined
}

const IncidentContext = React.createContext<IncidentContextType | undefined>(
  undefined
)

export function IncidentProvider({ children }: { children: React.ReactNode }) {
  const [incidents, setIncidents] = React.useState<Incident[]>([])
  const [predictions, setPredictions] = React.useState<PredictionDecision[]>([])
  const [isLoading, setIsLoading] = React.useState<boolean>(false)
  const [isOnline, setIsOnline] = React.useState<boolean>(false)
  const [isDemoMode, setIsDemoMode] = React.useState<boolean>(false)
  const [lastSync, setLastSync] = React.useState<Date | null>(null)

  // Check URL param or localStorage on mount
  React.useEffect(() => {
    const timer = setTimeout(() => {
      setLastSync(new Date())
      try {
        const params = new URLSearchParams(window.location.search)
        const demoParam = params.get("demo") === "1"
        const savedDemo = localStorage.getItem("nexus_demo_mode") === "true"
        if (demoParam || savedDemo) {
          setIsDemoMode(true)
          setIncidents(SAMPLE_INCIDENTS)
          setPredictions(SAMPLE_PREDICTIONS)
          setIsOnline(true)
        } else {
          setIsDemoMode(false)
          setIncidents([])
          setPredictions([])
        }
      } catch {
        // ignore SSR or storage errors
      }
    }, 0)
    return () => clearTimeout(timer)
  }, [])

  const fetchFromApi = React.useCallback(async () => {
    if (isDemoMode) {
      setLastSync(new Date())
      return
    }

    try {
      setIsLoading(true)
      const [incRes, predRes] = await Promise.all([
        fetch("/developer/incidents?n=50", { cache: "no-store" }),
        fetch("/developer/predictions?n=50", { cache: "no-store" }).catch(
          () => null
        ),
      ])

      if (!incRes.ok) throw new Error(`HTTP ${incRes.status}`)

      const data = await incRes.json()
      if (Array.isArray(data)) {
        setIncidents(data.map(normalizeIncident))
      } else {
        setIncidents([])
      }

      if (predRes && predRes.ok) {
        const predData = await predRes.json()
        if (Array.isArray(predData)) {
          setPredictions(predData)
        } else {
          setPredictions([])
        }
      } else {
        setPredictions([])
      }

      setIsOnline(true)
      setLastSync(new Date())
    } catch {
      // In live mode, NEVER fall back to mock data
      setIsOnline(false)
      setIncidents([])
      setPredictions([])
    } finally {
      setIsLoading(false)
    }
  }, [isDemoMode])

  // Polling loop
  React.useEffect(() => {
    const timer = setTimeout(() => {
      fetchFromApi()
    }, 0)

    const interval = setInterval(() => {
      if (document.visibilityState === "visible") {
        fetchFromApi()
      }
    }, 5000)

    return () => {
      clearTimeout(timer)
      clearInterval(interval)
    }
  }, [fetchFromApi])

  const toggleDemoMode = React.useCallback((enable: boolean) => {
    setIsDemoMode(enable)
    try {
      localStorage.setItem("nexus_demo_mode", String(enable))
    } catch {
      // ignore
    }
    if (enable) {
      setIncidents(SAMPLE_INCIDENTS)
      setPredictions(SAMPLE_PREDICTIONS)
      setIsOnline(true)
      setLastSync(new Date())
    } else {
      setIncidents([])
      setPredictions([])
      fetchFromApi()
    }
  }, [fetchFromApi])

  const approveIncident = React.useCallback(
    async (incidentId: string, reason = "Approved from NEXUS control plane") => {
      // Optimistic update
      setIncidents((prev) =>
        prev.map((inc) => {
          if (inc.incident_id !== incidentId) return inc
          return {
            ...inc,
            status: "RESOLVED",
            active_node: "LEARN",
            approval: {
              ...inc.approval,
              is_interrupted: false,
              approved: true,
              approved_by: "dashboard-operator",
              approval_status: "APPROVED",
              approval_reason: reason,
            },
            execution: {
              ...inc.execution,
              execution_status: "EXECUTED",
              remediation_attempts: inc.execution.remediation_attempts + 1,
              results: [
                ...inc.execution.results,
                {
                  step: (inc.execution.results.length || 0) + 1,
                  tool_name:
                    inc.remediation_plan.steps[0]?.tool_name ||
                    "k8s_remediate_resource",
                  status: "SUCCESS",
                  duration_ms: 820,
                  output: `Remediation executed successfully for ${inc.target_resource}`,
                },
              ],
            },
            verification: {
              verification_passed: true,
              verification_feedback:
                "Closed-loop verification passed: Health probes returning 200 OK across ready replicas.",
            },
            post_incident_report_ready: true,
          }
        })
      )

      if (isDemoMode) return true

      try {
        const res = await fetch(
          `/developer/incidents/${encodeURIComponent(incidentId)}/approve`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              incident_id: incidentId,
              approved: true,
              approver: "dashboard-operator",
              reason,
            }),
          }
        )
        return res.ok
      } catch {
        return false
      }
    },
    [isDemoMode]
  )

  const rejectIncident = React.useCallback(
    async (
      incidentId: string,
      reason = "Rejected and escalated from NEXUS control plane"
    ) => {
      // Optimistic update
      setIncidents((prev) =>
        prev.map((inc) => {
          if (inc.incident_id !== incidentId) return inc
          return {
            ...inc,
            status: "ESCALATED",
            active_node: "ESCALATE",
            approval: {
              ...inc.approval,
              is_interrupted: false,
              approved: false,
              approved_by: "dashboard-operator",
              approval_status: "REJECTED",
              approval_reason: reason,
            },
            error: `Operator rejected remediation: ${reason}`,
          }
        })
      )

      if (isDemoMode) return true

      try {
        const res = await fetch(
          `/developer/incidents/${encodeURIComponent(incidentId)}/reject`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              incident_id: incidentId,
              approved: false,
              approver: "dashboard-operator",
              reason,
            }),
          }
        )
        return res.ok
      } catch {
        return false
      }
    },
    [isDemoMode]
  )

  const getIncident = React.useCallback(
    (incidentId: string) => {
      return incidents.find((i) => i.incident_id === incidentId)
    },
    [incidents]
  )

  const value = React.useMemo(
    () => ({
      incidents,
      predictions,
      isLoading,
      isOnline,
      isDemoMode,
      lastSync,
      toggleDemoMode,
      refreshData: fetchFromApi,
      approveIncident,
      rejectIncident,
      getIncident,
    }),
    [
      incidents,
      predictions,
      isLoading,
      isOnline,
      isDemoMode,
      lastSync,
      toggleDemoMode,
      fetchFromApi,
      approveIncident,
      rejectIncident,
      getIncident,
    ]
  )

  return (
    <IncidentContext.Provider value={value}>
      {children}
    </IncidentContext.Provider>
  )
}

export function useIncidents() {
  const context = React.useContext(IncidentContext)
  if (!context) {
    throw new Error("useIncidents must be used within an IncidentProvider")
  }
  return context
}
