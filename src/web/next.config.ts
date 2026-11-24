import type { NextConfig } from "next";

/**
 * All NEXUS API traffic is issued server-side (Server Components and Server
 * Actions), so the browser never calls the API and no rewrite proxy is needed.
 * The base URL is read in `src/lib/nexus/client.ts` from `NEXUS_API_BASE_URL`.
 */
const nextConfig: NextConfig = {
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
