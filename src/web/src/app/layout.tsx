import type { Metadata } from "next"
import { Geist, Geist_Mono } from "next/font/google"
import "./globals.css"
import { cn } from "@/lib/utils"
import { IncidentProvider } from "@/context/incident-context"

const geistSans = Geist({
  variable: "--font-sans",
  subsets: ["latin"],
})

const geistMono = Geist_Mono({
  variable: "--font-mono",
  subsets: ["latin"],
})

export const metadata: Metadata = {
  title: "NEXUS Autonomous SRE Control Plane",
  description:
    "Self-healing Kubernetes incident remediation and predictive pod autoscaling control plane",
  icons: {
    icon: [
      { url: "/logo-dark.svg", media: "(prefers-color-scheme: light)" },
      { url: "/logo-light.svg", media: "(prefers-color-scheme: dark)" },
    ],
  },
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html
      lang="en"
      className={cn(
        "h-full",
        "antialiased",
        geistSans.variable,
        geistMono.variable,
        "font-sans"
      )}
    >
      <body className="min-h-full flex flex-col bg-background text-foreground antialiased selection:bg-primary/20 selection:text-primary">
        <IncidentProvider>{children}</IncidentProvider>
      </body>
    </html>
  )
}
