import * as React from "react"

import { ControlShell } from "@/components/control-shell"

export default function ControlLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return <ControlShell>{children}</ControlShell>
}
