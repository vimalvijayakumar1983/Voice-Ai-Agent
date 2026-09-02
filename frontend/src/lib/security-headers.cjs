'use strict';

function normalizeLiveKitConnectOrigin(value) {
  if (!value) return null;
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error('LIVEKIT_BROWSER_CONNECT_ORIGIN must be an absolute URL.');
  }
  if (!['wss:', 'ws:', 'https:', 'http:'].includes(url.protocol)) {
    throw new Error('LIVEKIT_BROWSER_CONNECT_ORIGIN must use HTTP(S) or WS(S).');
  }
  if (url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('LIVEKIT_BROWSER_CONNECT_ORIGIN must be an origin without credentials, path, query, or fragment.');
  }
  return url.origin;
}

function buildContentSecurityPolicy({ nonce, production, livekitConnectOrigin }) {
  if (!/^[A-Za-z0-9_-]{16,}$/.test(nonce)) {
    throw new Error('A strong CSP nonce is required.');
  }

  const connectSources = new Set([
    "'self'",
    'wss://api.smallest.ai',
    // LiveKit Cloud discovers a nearby regional host at runtime, so both the
    // HTTPS region lookup and its WebSocket endpoints must be trusted. Tokens
    // remain short-lived, room-scoped, and issued only by VAV's backend.
    'https://*.livekit.cloud',
    'wss://*.livekit.cloud',
  ]);
  const configuredLiveKitOrigin = normalizeLiveKitConnectOrigin(livekitConnectOrigin);
  if (configuredLiveKitOrigin) {
    const parsed = new URL(configuredLiveKitOrigin);
    if (production && !['wss:', 'https:'].includes(parsed.protocol)) {
      throw new Error('LIVEKIT_BROWSER_CONNECT_ORIGIN must use HTTPS or WSS in production.');
    }
    connectSources.add(configuredLiveKitOrigin);
    if (parsed.protocol === 'wss:') connectSources.add(configuredLiveKitOrigin.replace(/^wss:/, 'https:'));
    if (parsed.protocol === 'ws:') connectSources.add(configuredLiveKitOrigin.replace(/^ws:/, 'http:'));
    if (parsed.protocol === 'https:') connectSources.add(configuredLiveKitOrigin.replace(/^https:/, 'wss:'));
    if (parsed.protocol === 'http:') connectSources.add(configuredLiveKitOrigin.replace(/^http:/, 'ws:'));
  }
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

module.exports = { buildContentSecurityPolicy, normalizeLiveKitConnectOrigin };
