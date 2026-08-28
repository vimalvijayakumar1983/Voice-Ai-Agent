export interface SessionBoundary {
  epoch: number;
  accessToken: string | null;
}

export type RefreshResult =
  | {
      status: 'rotated';
      source: SessionBoundary;
      result: SessionBoundary;
    }
  | { status: 'session_changed' }
  | { status: 'failed' };

export function sameSessionBoundary(
  left: SessionBoundary | null,
  right: SessionBoundary | null,
): boolean;

export function canReplayAfterRefresh(
  requestBoundary: SessionBoundary,
  refreshResult: RefreshResult,
  currentBoundary: SessionBoundary,
): boolean;
