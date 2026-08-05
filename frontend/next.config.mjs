/** @type {import('next').NextConfig} */

// Two ways this app is served.
//
// Locally: a Next server that rewrites /api to the scheduler, so the browser
// stays same-origin and there is no CORS story to get wrong.
//
// On GitHub Pages: a static export under a repo subpath, talking to the
// backend across origins. NEXT_PUBLIC_API_BASE is what switches it, and it is
// baked in at build time.
const API = process.env.LABSCHED_API ?? "http://127.0.0.1:8791";
const EXPORT = process.env.LABSCHED_EXPORT === "1";

const nextConfig = EXPORT
  ? {
      output: "export",
      basePath: process.env.LABSCHED_BASE_PATH ?? "",
      images: { unoptimized: true },
      trailingSlash: true,
    }
  : {
      async rewrites() {
        return [{ source: "/api/:path*", destination: `${API}/api/:path*` }];
      },
    };

export default nextConfig;
