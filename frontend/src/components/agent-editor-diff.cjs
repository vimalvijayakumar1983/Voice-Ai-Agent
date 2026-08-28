'use strict';

function valuesEqual(left, right) {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length
      && left.every((value, index) => valuesEqual(value, right[index]));
  }
  if (
    left
    && right
    && typeof left === 'object'
    && typeof right === 'object'
    && !Array.isArray(left)
    && !Array.isArray(right)
  ) {
    const leftKeys = Object.keys(left);
    const rightKeys = Object.keys(right);
    return leftKeys.length === rightKeys.length
      && leftKeys.every((key) => Object.hasOwn(right, key) && valuesEqual(left[key], right[key]));
  }
  return false;
}

function agentEditorPatch(original, current) {
  const patch = {};
  for (const key of Object.keys(current)) {
    if (!valuesEqual(original[key], current[key])) patch[key] = current[key];
  }
  return patch;
}

module.exports = { agentEditorPatch, valuesEqual };
