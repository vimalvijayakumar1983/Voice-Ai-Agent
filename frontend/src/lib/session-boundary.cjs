'use strict';

function sameSessionBoundary(left, right) {
  return Boolean(
    left
      && right
      && left.epoch === right.epoch
      && left.accessToken === right.accessToken
      && left.refreshToken === right.refreshToken,
  );
}

function canReplayAfterRefresh(requestBoundary, refreshResult, currentBoundary) {
  return Boolean(
    refreshResult
      && refreshResult.status === 'rotated'
      && sameSessionBoundary(requestBoundary, refreshResult.source)
      && sameSessionBoundary(refreshResult.result, currentBoundary),
  );
}

module.exports = {
  canReplayAfterRefresh,
  sameSessionBoundary,
};
