export type WebhookReplayAvailability =
  | { enabled: true; reason: null }
  | { enabled: false; reason: string };

export function webhookReplayAvailability(
  isActive: boolean,
  deliveryStatus: string,
): WebhookReplayAvailability;

export function webhookUndeliveredResultLabel(lastError: string | null): string;
