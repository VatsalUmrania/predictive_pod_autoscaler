# NEXUS Control Plane

Next.js front end for the NEXUS autonomous SRE engine. Every value on a live page
comes from the NEXUS API. There is no mock-data fallback: when the engine cannot be
reached, the page says so and offers a retry.

## Running it

The web app needs the NEXUS API running.

```bash
# from the repository root
nexus server start --port 8080
curl -s localhost:8080/health

# then, from src/web
npm install
npm run dev        # http://localhost:3000
```

Other commands:

```bash
npm run lint       # eslint (next lint was removed in Next.js 16)
npm run build      # production build. Also the gate for cacheComponents errors.
npm run start      # serve the production build
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `NEXUS_API_BASE_URL` | `http://127.0.0.1:8080` | Base URL of the NEXUS FastAPI server |

Copy `.env.example` to `.env.local` to override it. The variable is read on the
server only, in `src/lib/nexus/client.ts`. All API traffic originates in a React
Server Component or a Server Action, so the browser never calls the API and no
rewrite proxy is configured.

If the container's base URL is only known at runtime rather than at build time, add
`await connection()` from `next/server` at the top of `nexusBaseUrl()`.

## How the data layer works

- `src/lib/nexus/types.ts` mirrors the API responses exactly. Fields the API does not
  send are `null` or optional, never filled with a placeholder value.
- `src/lib/nexus/client.ts` has one function per endpoint, all `cache: "no-store"`.
  Each is wrapped in React's `cache()` so repeated calls within one request are
  deduplicated.
- `src/lib/nexus/actions.ts` holds the Server Actions for approve, reject and the
  prescaler mode switch. They return the engine's real response, which the UI
  renders verbatim.
- Every page is a Server Component that awaits only its own endpoints, inside a
  `<Suspense>` boundary with a shape-matched skeleton. With `cacheComponents`
  enabled, an uncached read outside a Suspense boundary fails the build.

## Routes

| Route | Endpoints | Contents |
|---|---|---|
| `/` | `/health`, `/status`, `/developer/incidents`, `/approvals/pending`, `/rca/last` | Engine status, workflow counters, prescaler and learning metrics, pending approvals, recent incidents, recent RCA |
| `/incidents` | `/developer/incidents` | Filterable and searchable incident feed |
| `/incidents/[id]` | `/developer/incidents/{id}`, `/agent-runs`, `/messages`, `/report`, `/audit/incident/{id}` | Pipeline stepper, diagnosis, plan, execution, agent activity, audit trail, post-incident report |
| `/approvals` | `/approvals/pending`, `/developer/incidents` | Human approval queue with approve and reject |
| `/predictions` | `/developer/predictions`, `/status` | Scaling decisions and the autonomy mode switch |
| `/audit` | `/audit/tail` | Governance audit trail |
| `/reports` | `/developer/incidents` | Incidents with a generated post-incident report |
| `/policy` | `/developer/incidents`, `/developer/policy/{app}` | Resolved self-heal policy per application |
| `/demo/*` | none | Static fixture rendering the same components, behind a persistent "sample data" banner |

## Design system

shadcn/ui in the `base-mira` style on `@base-ui/react` primitives, with HugeIcons.
The token set lives in `src/app/globals.css` (oklch neutrals, Geist and Geist Mono,
`--radius: 0.5rem`, full light and dark sets).

Colour carries a fixed meaning across every page:

| Meaning | Token |
|---|---|
| Needs operator attention | `--chart-2` |
| Failure, escalated, rolled back | `--destructive` |
| Resolved and verified | `--primary` |
| Sample data (demo routes only) | `--chart-1` |

`server-only` is not a dependency, so the API client is not guarded by an import
guard. Keep it out of Client Components.

## Conventions

- Numbers are rendered in Geist Mono with `tabular-nums`.
- Status pills come from `src/components/dashboard/status-badge.tsx` so the colour
  mapping stays consistent.
- Timestamps are formatted in UTC by `src/lib/format.ts` so server output and client
  hydration match exactly.
