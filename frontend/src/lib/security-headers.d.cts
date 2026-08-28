export function apiOrigin(apiUrl: string): string;

export function buildContentSecurityPolicy(options: {
  nonce: string;
  apiUrl: string;
  production: boolean;
}): string;
