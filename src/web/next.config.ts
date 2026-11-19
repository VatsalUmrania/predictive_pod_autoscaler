import type { NextConfig } from "next";

const nexusApiUrl = process.env.NEXUS_API_URL;

const nextConfig: NextConfig = {
  /* config options here */
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
  async rewrites() {
    return [
      {
        source: "/developer/:path*",
        destination: `${nexusApiUrl}/developer/:path*`,
      },
      {
        source: "/api/v1/:path*",
        destination: `${nexusApiUrl}/api/v1/:path*`,
      },
    ];
  },
};

export default nextConfig;
