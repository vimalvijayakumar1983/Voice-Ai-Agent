export function buildContentSecurityPolicy(options: {
  nonce: string;
  production: boolean;
  livekitConnectOrigin?: string;
}): string;

export function normalizeLiveKitConnectOrigin(value?: string): string | null;
