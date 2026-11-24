"use client"

import * as React from "react"
import { useRouter } from "next/navigation"

import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"

export function PolicyPicker({
  apps,
  selected,
}: {
  apps: string[]
  selected?: string
}) {
  const router = useRouter()
  const [optimistic, setOptimistic] = React.useOptimistic(
    selected ?? "",
    (_current: string, next: string) => next
  )
  const [, startTransition] = React.useTransition()

  const onChange = (next: string) => {
    startTransition(() => {
      setOptimistic(next)
      router.push(next ? `/policy?app=${encodeURIComponent(next)}` : "/policy")
    })
  }

  return (
    <NativeSelect
      aria-label="Select an application"
      value={optimistic}
      onChange={(event) => onChange(event.target.value)}
      className="w-full sm:w-72"
    >
      <NativeSelectOption value="">Select an application</NativeSelectOption>
      {apps.map((app) => (
        <NativeSelectOption key={app} value={app} className="font-mono text-xs">
          {app}
        </NativeSelectOption>
      ))}
    </NativeSelect>
  )
}
