import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import securityHeaders from '../src/lib/security-headers.cjs';

const { buildContentSecurityPolicy } = securityHeaders;

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
});

test('production CSP uses a nonce and exact voice/API transport origins', () => {
  const policy = buildContentSecurityPolicy({
    nonce: '0123456789abcdef0123456789abcdef',
    apiUrl: 'https://api.voice.example/path-is-ignored',
    production: true,
  });

  assert.match(
    policy,
    /script-src 'self' 'nonce-0123456789abcdef0123456789abcdef' 'strict-dynamic'/,
  );
  assert.doesNotMatch(policy, /script-src[^;]*'unsafe-inline'/);
  assert.match(
    policy,
    /connect-src 'self' https:\/\/api\.voice\.example wss:\/\/api\.smallest\.ai/,
  );
  assert.doesNotMatch(policy, /connect-src[^;]*(?:https:|wss:)(?:;|\s*$)/);
  assert.match(policy, /media-src 'self' blob:/);
  assert.doesNotMatch(policy, /media-src[^;]*https:/);
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
