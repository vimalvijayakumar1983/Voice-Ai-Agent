import test from 'node:test';
import assert from 'node:assert/strict';

import sessionBoundary from '../src/lib/session-boundary.cjs';

const { canReplayAfterRefresh } = sessionBoundary;

test('an old POST is not replayed after a cross-tenant login replaces the session', () => {
  const tenantARequest = {
    epoch: 10,
    accessToken: 'tenant-a-access',
    refreshToken: 'tenant-a-refresh',
  };
  const tenantARotation = {
    status: 'rotated',
    source: tenantARequest,
    result: {
      epoch: 11,
      accessToken: 'tenant-a-rotated-access',
      refreshToken: 'tenant-a-rotated-refresh',
    },
  };
  const tenantBLogin = {
    epoch: 12,
    accessToken: 'tenant-b-access',
    refreshToken: 'tenant-b-refresh',
  };

  assert.equal(
    canReplayAfterRefresh(tenantARequest, tenantARotation, tenantBLogin),
    false,
  );
});

test('a request may replay only under its exact successful refresh rotation', () => {
  const request = {
    epoch: 4,
    accessToken: 'old-access',
    refreshToken: 'old-refresh',
  };
  const rotated = {
    epoch: 5,
    accessToken: 'rotated-access',
    refreshToken: 'rotated-refresh',
  };

  assert.equal(
    canReplayAfterRefresh(
      request,
      { status: 'rotated', source: request, result: rotated },
      rotated,
    ),
    true,
  );
});

test('a refresh proof from a different source session is rejected', () => {
  const request = {
    epoch: 7,
    accessToken: 'request-access',
    refreshToken: 'request-refresh',
  };
  const otherSource = {
    epoch: 7,
    accessToken: 'other-access',
    refreshToken: 'other-refresh',
  };
  const rotated = {
    epoch: 8,
    accessToken: 'rotated-access',
    refreshToken: 'rotated-refresh',
  };

  assert.equal(
    canReplayAfterRefresh(
      request,
      { status: 'rotated', source: otherSource, result: rotated },
      rotated,
    ),
    false,
  );
});
