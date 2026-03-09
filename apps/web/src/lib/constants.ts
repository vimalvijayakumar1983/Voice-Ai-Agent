export const APP_NAME = 'VoiceAI';
export const APP_DESCRIPTION = 'Enterprise Voice AI Agent Platform';

export const CALL_STATUSES = {
  QUEUED: 'queued',
  RINGING: 'ringing',
  IN_PROGRESS: 'in-progress',
  COMPLETED: 'completed',
  FAILED: 'failed',
  NO_ANSWER: 'no-answer',
  BUSY: 'busy',
  CANCELED: 'canceled',
} as const;

export const CALL_STATUS_COLORS: Record<string, string> = {
  queued: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400',
  ringing: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400',
  'in-progress': 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400',
  completed: 'bg-slate-100 text-slate-800 dark:bg-slate-900/30 dark:text-slate-400',
  failed: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400',
  'no-answer': 'bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-400',
  busy: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-400',
  canceled: 'bg-gray-100 text-gray-800 dark:bg-gray-900/30 dark:text-gray-400',
};

export const CAMPAIGN_STATUSES = {
  DRAFT: 'draft',
  SCHEDULED: 'scheduled',
  RUNNING: 'running',
  PAUSED: 'paused',
  COMPLETED: 'completed',
  CANCELED: 'canceled',
} as const;

export const AGENT_TYPES = {
  OUTBOUND_SALES: 'outbound-sales',
  INBOUND_SUPPORT: 'inbound-support',
  APPOINTMENT_BOOKING: 'appointment-booking',
  SURVEY: 'survey',
  LEAD_QUALIFICATION: 'lead-qualification',
  COLLECTIONS: 'collections',
  CUSTOM: 'custom',
} as const;

export const INDUSTRIES = [
  'Healthcare',
  'Real Estate',
  'Financial Services',
  'Insurance',
  'Technology',
  'E-commerce',
  'Education',
  'Legal',
  'Automotive',
  'Travel & Hospitality',
  'Other',
] as const;

export const VOICE_OPTIONS = [
  { id: 'alloy', name: 'Alloy', gender: 'Neutral', accent: 'American' },
  { id: 'echo', name: 'Echo', gender: 'Male', accent: 'American' },
  { id: 'fable', name: 'Fable', gender: 'Male', accent: 'British' },
  { id: 'onyx', name: 'Onyx', gender: 'Male', accent: 'American' },
  { id: 'nova', name: 'Nova', gender: 'Female', accent: 'American' },
  { id: 'shimmer', name: 'Shimmer', gender: 'Female', accent: 'American' },
] as const;

export const LANGUAGES = [
  { code: 'en-US', name: 'English (US)' },
  { code: 'en-GB', name: 'English (UK)' },
  { code: 'es-ES', name: 'Spanish' },
  { code: 'fr-FR', name: 'French' },
  { code: 'de-DE', name: 'German' },
  { code: 'pt-BR', name: 'Portuguese (Brazil)' },
  { code: 'ja-JP', name: 'Japanese' },
  { code: 'zh-CN', name: 'Chinese (Mandarin)' },
] as const;

export const ITEMS_PER_PAGE = 25;
