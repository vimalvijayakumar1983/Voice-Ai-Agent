'use strict';

function normalizeApiProxyTarget(value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error('API proxy target must be an absolute HTTP(S) origin.');
  }

  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
    throw new Error('API proxy target must use HTTP or HTTPS.');
  }
  if (
    parsed.username
    || parsed.password
    || parsed.pathname !== '/'
    || parsed.search
    || parsed.hash
  ) {
    throw new Error('API proxy target must be an origin without credentials, path, query, or fragment.');
  }

  return parsed.origin;
}

module.exports = { normalizeApiProxyTarget };
