import apiProxyTargetHelpers from './src/lib/api-proxy-target.cjs';

const { normalizeApiProxyTarget } = apiProxyTargetHelpers;

// Browser API traffic stays on the frontend origin so the rotating HttpOnly
// refresh cookie remains first-party even when Railway assigns the services
// different public hostnames. The external destination is fixed at build time
// and is never derived from a request.
const apiProxyTarget = normalizeApiProxyTarget(
  process.env.API_PROXY_TARGET
    || process.env.NEXT_PUBLIC_API_URL
    || 'http://localhost:8000',
);

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  async rewrites() {
    return [
      {
        source: '/api/v1/:path*',
        destination: `${apiProxyTarget}/api/v1/:path*`,
      },
    ];
  },
  async headers() {
    const isProduction = process.env.NODE_ENV === 'production';
    const securityHeaders = [
      { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
      { key: 'X-Content-Type-Options', value: 'nosniff' },
      { key: 'X-Frame-Options', value: 'DENY' },
      { key: 'Permissions-Policy', value: 'camera=(), geolocation=(), microphone=(self), payment=()' },
    ];
    if (isProduction) {
      securityHeaders.push({
        key: 'Strict-Transport-Security',
        value: 'max-age=31536000; includeSubDomains',
      });
    }

    return [
      {
        source: '/(.*)',
        headers: securityHeaders,
      },
    ];
  },
};

export default nextConfig;
