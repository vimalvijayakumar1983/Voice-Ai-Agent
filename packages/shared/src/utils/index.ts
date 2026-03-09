// ============================================================
// Voice AI Agent Platform - Shared Utilities
// ============================================================

import { PATTERNS, ROLE_HIERARCHY, TERMINAL_CALL_STATUSES } from '../constants';
import type { PaginationParams, PaginatedResponse } from '../types';
import { UserRole, CallStatus } from '../types';

// ---------------------
// String Utilities
// ---------------------

/**
 * Generate a URL-friendly slug from a string.
 */
export function slugify(text: string): string {
  return text
    .toLowerCase()
    .trim()
    .replace(/[^\w\s-]/g, '')
    .replace(/[\s_]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

/**
 * Truncate a string to a maximum length with an ellipsis.
 */
export function truncate(text: string, maxLength: number): string {
  if (text.length <= maxLength) return text;
  return text.slice(0, maxLength - 3) + '...';
}

/**
 * Capitalize the first letter of a string.
 */
export function capitalize(text: string): string {
  if (!text) return text;
  return text.charAt(0).toUpperCase() + text.slice(1).toLowerCase();
}

/**
 * Convert an enum-style string to a human-readable label.
 * Example: "INBOUND_SUPPORT" -> "Inbound Support"
 */
export function enumToLabel(value: string): string {
  return value
    .split('_')
    .map((word) => capitalize(word))
    .join(' ');
}

// ---------------------
// Validation Utilities
// ---------------------

/**
 * Validate an E.164 phone number format.
 */
export function isValidE164Phone(phone: string): boolean {
  return PATTERNS.E164_PHONE.test(phone);
}

/**
 * Validate an email address format.
 */
export function isValidEmail(email: string): boolean {
  return PATTERNS.EMAIL.test(email);
}

/**
 * Validate a UUID format.
 */
export function isValidUUID(id: string): boolean {
  return PATTERNS.UUID.test(id);
}

/**
 * Validate a URL-friendly slug.
 */
export function isValidSlug(slug: string): boolean {
  return PATTERNS.SLUG.test(slug);
}

// ---------------------
// Phone Utilities
// ---------------------

/**
 * Normalize a phone number to E.164 format (basic normalization).
 * Strips spaces, dashes, parentheses. Prepends + if missing.
 */
export function normalizePhoneNumber(phone: string): string {
  const cleaned = phone.replace(/[\s\-()]/g, '');
  if (cleaned.startsWith('+')) return cleaned;
  return `+${cleaned}`;
}

/**
 * Mask a phone number for display (e.g., +1234567890 -> +1***7890).
 */
export function maskPhoneNumber(phone: string): string {
  if (phone.length < 8) return phone;
  const prefix = phone.slice(0, 2);
  const suffix = phone.slice(-4);
  return `${prefix}***${suffix}`;
}

// ---------------------
// Role / Permission Utilities
// ---------------------

/**
 * Check if a role has equal or higher authority than another role.
 */
export function hasRoleAuthority(userRole: UserRole, requiredRole: UserRole): boolean {
  return (ROLE_HIERARCHY[userRole] ?? 0) >= (ROLE_HIERARCHY[requiredRole] ?? 0);
}

/**
 * Check if a role is at least admin level.
 */
export function isAdminRole(role: UserRole): boolean {
  return hasRoleAuthority(role, UserRole.ADMIN);
}

// ---------------------
// Call Utilities
// ---------------------

/**
 * Check if a call status is terminal (no further transitions possible).
 */
export function isTerminalCallStatus(status: CallStatus): boolean {
  return TERMINAL_CALL_STATUSES.includes(status);
}

/**
 * Format call duration in seconds to a human-readable string.
 * Example: 125 -> "2m 5s"
 */
export function formatCallDuration(seconds: number): string {
  if (seconds < 0) return '0s';
  const hrs = Math.floor(seconds / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  const secs = Math.floor(seconds % 60);

  const parts: string[] = [];
  if (hrs > 0) parts.push(`${hrs}h`);
  if (mins > 0) parts.push(`${mins}m`);
  parts.push(`${secs}s`);

  return parts.join(' ');
}

// ---------------------
// Pagination Utilities
// ---------------------

/**
 * Build a PaginatedResponse wrapper from a data array and total count.
 */
export function buildPaginatedResponse<T>(
  data: T[],
  total: number,
  params: PaginationParams,
): PaginatedResponse<T> {
  const totalPages = Math.ceil(total / params.limit);
  return {
    data,
    meta: {
      total,
      page: params.page,
      limit: params.limit,
      totalPages,
      hasNextPage: params.page < totalPages,
      hasPreviousPage: params.page > 1,
    },
  };
}

/**
 * Calculate SQL-style offset from pagination params.
 */
export function paginationToOffset(params: PaginationParams): { skip: number; take: number } {
  return {
    skip: (params.page - 1) * params.limit,
    take: params.limit,
  };
}

// ---------------------
// Date / Time Utilities
// ---------------------

/**
 * Format a date to ISO string (safe for null/undefined).
 */
export function toISOString(date: Date | string | null | undefined): string | null {
  if (!date) return null;
  if (typeof date === 'string') return new Date(date).toISOString();
  return date.toISOString();
}

/**
 * Get the current Unix timestamp in seconds.
 */
export function nowUnixSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

/**
 * Check if a date is in the past.
 */
export function isInPast(date: Date | string): boolean {
  const d = typeof date === 'string' ? new Date(date) : date;
  return d.getTime() < Date.now();
}

// ---------------------
// Misc Utilities
// ---------------------

/**
 * Sleep for a given number of milliseconds.
 */
export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Safely parse JSON, returning a default value on failure.
 */
export function safeJsonParse<T>(json: string, fallback: T): T {
  try {
    return JSON.parse(json) as T;
  } catch {
    return fallback;
  }
}

/**
 * Remove undefined values from an object (shallow).
 */
export function removeUndefined<T extends Record<string, unknown>>(obj: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(obj).filter(([, v]) => v !== undefined),
  ) as Partial<T>;
}

/**
 * Generate a deterministic hash code from a string (non-cryptographic).
 */
export function hashCode(str: string): number {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    const char = str.charCodeAt(i);
    hash = (hash << 5) - hash + char;
    hash |= 0; // Convert to 32-bit integer
  }
  return Math.abs(hash);
}

/**
 * Chunk an array into smaller arrays of a given size.
 */
export function chunk<T>(array: T[], size: number): T[][] {
  const chunks: T[][] = [];
  for (let i = 0; i < array.length; i += size) {
    chunks.push(array.slice(i, i + size));
  }
  return chunks;
}

/**
 * Retry a function with exponential backoff.
 */
export async function retryWithBackoff<T>(
  fn: () => Promise<T>,
  options: { maxRetries?: number; baseDelayMs?: number; maxDelayMs?: number } = {},
): Promise<T> {
  const { maxRetries = 3, baseDelayMs = 1000, maxDelayMs = 30_000 } = options;

  let lastError: Error | undefined;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await fn();
    } catch (error) {
      lastError = error as Error;
      if (attempt < maxRetries) {
        const delay = Math.min(baseDelayMs * 2 ** attempt, maxDelayMs);
        const jitter = delay * 0.1 * Math.random();
        await sleep(delay + jitter);
      }
    }
  }
  throw lastError;
}
