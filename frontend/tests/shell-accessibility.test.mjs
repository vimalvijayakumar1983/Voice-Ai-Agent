import test from 'node:test';
import assert from 'node:assert/strict';

import shellAccessibility from '../src/lib/shell-accessibility.cjs';

const {
  E164_PATTERN,
  PAGE_TITLES,
  focusTrapTarget,
  pageTitleForPath,
} = shellAccessibility;

test('workspace routes expose unique, descriptive document titles', () => {
  const titles = Object.values(PAGE_TITLES);
  assert.equal(new Set(titles).size, titles.length);
  assert.equal(pageTitleForPath('/compliance'), 'Compliance');
  assert.equal(pageTitleForPath('/unknown'), 'Workspace');
});

test('mobile navigation focus wraps at both boundaries', () => {
  assert.equal(focusTrapTarget(0, 4, true), 3);
  assert.equal(focusTrapTarget(3, 4, false), 0);
  assert.equal(focusTrapTarget(-1, 4, false), 0);
  assert.equal(focusTrapTarget(-1, 4, true), 3);
  assert.equal(focusTrapTarget(1, 4, false), null);
  assert.equal(focusTrapTarget(1, 4, true), null);
  assert.equal(focusTrapTarget(-1, 0, false), null);
});

test('compliance phone input accepts E.164-shaped values only', () => {
  const e164 = new RegExp(`^(?:${E164_PATTERN})$`);
  assert.equal(e164.test('+971501234567'), true);
  assert.equal(e164.test('+14155552671'), true);
  assert.equal(e164.test('0501234567'), false);
  assert.equal(e164.test('+012345678'), false);
  assert.equal(e164.test('+971 50 123 4567'), false);
});
