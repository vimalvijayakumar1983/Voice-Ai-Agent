import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import securityHeaders from '../src/lib/security-headers.cjs';
import apiProxyTarget from '../src/lib/api-proxy-target.cjs';

const { buildContentSecurityPolicy } = securityHeaders;
const { normalizeApiProxyTarget } = apiProxyTarget;

test('browser-readable storage never persists an access or refresh credential', () => {
  const apiSource = readFileSync(new URL('../src/lib/api.ts', import.meta.url), 'utf8');

  assert.doesNotMatch(
    apiSource,
    /localStorage\.setItem\(\s*['"](?:access_token|refresh_token)['"]/,
  );
  const authContract = /interface AuthTokens\s*\{([^}]*)\}/.exec(apiSource)?.[1] || '';
  assert.doesNotMatch(authContract, /refresh_token/);
  assert.match(apiSource, /localStorage\.removeItem\('access_token'\)/);
  assert.match(apiSource, /localStorage\.removeItem\('refresh_token'\)/);
  assert.match(apiSource, /credentials: 'include'/);
  assert.match(apiSource, /migrate-session/);
  assert.match(apiSource, /const API_URL = ''/);
  assert.doesNotMatch(apiSource, /process\.env\.NEXT_PUBLIC_API_URL/);
});

test('production CSP uses a nonce and only same-origin API plus exact voice transport', () => {
  const policy = buildContentSecurityPolicy({
    nonce: '0123456789abcdef0123456789abcdef',
    production: true,
  });

  assert.match(
    policy,
    /script-src 'self' 'nonce-0123456789abcdef0123456789abcdef' 'strict-dynamic'/,
  );
  assert.doesNotMatch(policy, /script-src[^;]*'unsafe-inline'/);
  assert.match(
    policy,
    /connect-src 'self' wss:\/\/api\.smallest\.ai/,
  );
  assert.doesNotMatch(policy, /connect-src[^;]*(?:https:|wss:)(?:;|\s*$)/);
  assert.match(policy, /media-src 'self' blob:/);
  assert.doesNotMatch(policy, /media-src[^;]*https:/);
});

test('Next proxies API traffic to one validated deployment origin', () => {
  const nextConfigSource = readFileSync(new URL('../next.config.mjs', import.meta.url), 'utf8');

  assert.match(nextConfigSource, /source: '\/api\/v1\/:path\*'/);
  assert.match(nextConfigSource, /destination: `\$\{apiProxyTarget\}\/api\/v1\/:path\*`/);
  assert.equal(normalizeApiProxyTarget('https://api.voice.example'), 'https://api.voice.example');
  assert.equal(normalizeApiProxyTarget('http://localhost:8000'), 'http://localhost:8000');
  assert.throws(() => normalizeApiProxyTarget('javascript:alert(1)'), /HTTP or HTTPS/);
  assert.throws(() => normalizeApiProxyTarget('https://user:secret@api.voice.example'), /origin without/);
  assert.throws(() => normalizeApiProxyTarget('https://api.voice.example/v1'), /origin without/);
  assert.throws(() => normalizeApiProxyTarget('https://api.voice.example?next=evil'), /origin without/);
});

test('Next request boundary forwards one nonce into Document scripts', () => {
  const proxySource = readFileSync(new URL('../src/proxy.ts', import.meta.url), 'utf8');
  const documentSource = readFileSync(new URL('../src/pages/_document.tsx', import.meta.url), 'utf8');
  const appSource = readFileSync(new URL('../src/pages/_app.tsx', import.meta.url), 'utf8');

  assert.match(proxySource, /requestHeaders\.set\('x-nonce', nonce\)/);
  assert.match(proxySource, /Content-Security-Policy/);
  assert.match(documentSource, /context\.req\?\.headers\['x-nonce'\]/);
  assert.match(documentSource, /<NextScript nonce=\{nonce\}/);
  assert.match(appSource, /App\.getInitialProps/);
});
