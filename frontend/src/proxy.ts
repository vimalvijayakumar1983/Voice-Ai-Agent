import { type NextRequest, NextResponse } from 'next/server';
import { buildContentSecurityPolicy } from '@/lib/security-headers.cjs';

export function proxy(request: NextRequest) {
  const nonce = crypto.randomUUID().replaceAll('-', '');
  const contentSecurityPolicy = buildContentSecurityPolicy({
    nonce,
    production: process.env.NODE_ENV === 'production',
    // Optional exact origin for self-hosted LiveKit. LiveKit Cloud regional
    // endpoints are already constrained to *.livekit.cloud by the CSP helper.
    livekitConnectOrigin: process.env.LIVEKIT_BROWSER_CONNECT_ORIGIN,
  });
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set('x-nonce', nonce);
  requestHeaders.set('Content-Security-Policy', contentSecurityPolicy);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set('Content-Security-Policy', contentSecurityPolicy);
  return response;
}

export const config = {
  matcher: [
    {
      source: '/((?!_next/static|_next/image|favicon.ico).*)',
      missing: [
        { type: 'header', key: 'next-router-prefetch' },
        { type: 'header', key: 'purpose', value: 'prefetch' },
      ],
    },
  ],
};
