'use strict';

function buildContentSecurityPolicy({ nonce, production }) {
  if (!/^[A-Za-z0-9_-]{16,}$/.test(nonce)) {
    throw new Error('A strong CSP nonce is required.');
  }

  const connectSources = new Set([
    "'self'",
    'wss://api.smallest.ai',
  ]);
  if (!production) {
    connectSources.add('ws://localhost:*');
    connectSources.add('http://localhost:*');
  }

  return [
    "default-src 'self'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "object-src 'none'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${production ? '' : " 'unsafe-eval'"}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "media-src 'self' blob:",
    `connect-src ${Array.from(connectSources).join(' ')}`,
    "worker-src 'self' blob:",
    ...(production ? ['upgrade-insecure-requests'] : []),
  ].join('; ');
}

module.exports = { buildContentSecurityPolicy };
