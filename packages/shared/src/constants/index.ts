// ============================================================
// Voice AI Agent Platform - Shared Constants
// ============================================================

import {
  UserRole,
  CallStatus,
  CampaignStatus,
  ContactStatus,
  ConsentStatus,
  Industry,
  AgentType,
  WorkflowNodeType,
} from '../types';

// ---------------------
// Application
// ---------------------

export const APP_NAME = 'VoiceAI';
export const APP_VERSION = '0.1.0';

// ---------------------
// Pagination
// ---------------------

export const DEFAULT_PAGE = 1;
export const DEFAULT_PAGE_SIZE = 20;
export const MAX_PAGE_SIZE = 100;

// ---------------------
// Call Limits
// ---------------------

export const MAX_CALL_DURATION_SECONDS = 3600; // 1 hour
export const DEFAULT_CALL_DURATION_SECONDS = 900; // 15 minutes
export const CALL_RING_TIMEOUT_SECONDS = 30;
export const MAX_CONCURRENT_CALLS_DEFAULT = 10;
export const CALLS_PER_MINUTE_DEFAULT = 5;

// ---------------------
// Campaign Limits
// ---------------------

export const MAX_RETRY_ATTEMPTS = 5;
export const DEFAULT_RETRY_ATTEMPTS = 3;
export const DEFAULT_RETRY_DELAY_MINUTES = 60;
export const MAX_CONTACTS_PER_CAMPAIGN = 100_000;

// ---------------------
// Auth
// ---------------------

export const PASSWORD_MIN_LENGTH = 8;
export const PASSWORD_MAX_LENGTH = 128;
export const MAX_LOGIN_ATTEMPTS = 5;
export const LOGIN_LOCKOUT_MINUTES = 15;
export const JWT_ACCESS_DEFAULT_EXPIRY = '15m';
export const JWT_REFRESH_DEFAULT_EXPIRY = '7d';

// ---------------------
// File Upload
// ---------------------

export const MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024; // 50 MB
export const ALLOWED_AUDIO_FORMATS = ['mp3', 'wav', 'ogg', 'webm', 'flac'];
export const ALLOWED_IMPORT_FORMATS = ['csv', 'xlsx', 'json'];
export const MAX_IMPORT_ROWS = 100_000;

// ---------------------
// Rate Limiting
// ---------------------

export const RATE_LIMIT_DEFAULT_TTL = 60; // seconds
export const RATE_LIMIT_DEFAULT_MAX = 100;
export const RATE_LIMIT_AUTH_TTL = 60;
export const RATE_LIMIT_AUTH_MAX = 10;

// ---------------------
// WebSocket
// ---------------------

export const WS_HEARTBEAT_INTERVAL = 30_000; // 30 seconds
export const WS_RECONNECT_INTERVAL = 5_000; // 5 seconds
export const WS_MAX_RECONNECT_ATTEMPTS = 10;

// ---------------------
// Queue Names
// ---------------------

export const QUEUE_NAMES = {
  OUTBOUND_CALLS: 'outbound-calls',
  CALL_PROCESSING: 'call-processing',
  RECORDING_PROCESSING: 'recording-processing',
  TRANSCRIPTION: 'transcription',
  CAMPAIGN_SCHEDULER: 'campaign-scheduler',
  WEBHOOK_DELIVERY: 'webhook-delivery',
  EMAIL_NOTIFICATION: 'email-notification',
  ANALYTICS_AGGREGATION: 'analytics-aggregation',
  CONTACT_IMPORT: 'contact-import',
} as const;

// ---------------------
// Redis Key Prefixes
// ---------------------

export const REDIS_PREFIXES = {
  SESSION: 'session:',
  RATE_LIMIT: 'rate_limit:',
  CALL_STATE: 'call_state:',
  ACTIVE_CALLS: 'active_calls:',
  CAMPAIGN_STATE: 'campaign_state:',
  DNC_LIST: 'dnc:',
  CACHE: 'cache:',
  LOCK: 'lock:',
  PUBSUB: 'pubsub:',
} as const;

// ---------------------
// S3 Paths
// ---------------------

export const S3_PATHS = {
  RECORDINGS: 'recordings',
  TRANSCRIPTS: 'transcripts',
  IMPORTS: 'imports',
  EXPORTS: 'exports',
  AVATARS: 'avatars',
  ASSETS: 'assets',
} as const;

// ---------------------
// Role Hierarchy (higher number = more permissions)
// ---------------------

export const ROLE_HIERARCHY: Record<UserRole, number> = {
  [UserRole.SUPER_ADMIN]: 100,
  [UserRole.TENANT_OWNER]: 90,
  [UserRole.ADMIN]: 80,
  [UserRole.MANAGER]: 70,
  [UserRole.SUPERVISOR]: 60,
  [UserRole.QA_AUDITOR]: 50,
  [UserRole.AGENT_OPERATOR]: 40,
  [UserRole.READONLY_ANALYST]: 10,
};

// ---------------------
// Status Display Labels
// ---------------------

export const CALL_STATUS_LABELS: Record<CallStatus, string> = {
  [CallStatus.QUEUED]: 'Queued',
  [CallStatus.RINGING]: 'Ringing',
  [CallStatus.IN_PROGRESS]: 'In Progress',
  [CallStatus.COMPLETED]: 'Completed',
  [CallStatus.FAILED]: 'Failed',
  [CallStatus.VOICEMAIL]: 'Voicemail',
  [CallStatus.NO_ANSWER]: 'No Answer',
  [CallStatus.BUSY]: 'Busy',
  [CallStatus.TRANSFERRED]: 'Transferred',
  [CallStatus.CANCELLED]: 'Cancelled',
};

export const CAMPAIGN_STATUS_LABELS: Record<CampaignStatus, string> = {
  [CampaignStatus.DRAFT]: 'Draft',
  [CampaignStatus.SCHEDULED]: 'Scheduled',
  [CampaignStatus.RUNNING]: 'Running',
  [CampaignStatus.PAUSED]: 'Paused',
  [CampaignStatus.COMPLETED]: 'Completed',
  [CampaignStatus.CANCELLED]: 'Cancelled',
};

export const CONTACT_STATUS_LABELS: Record<ContactStatus, string> = {
  [ContactStatus.ACTIVE]: 'Active',
  [ContactStatus.INACTIVE]: 'Inactive',
  [ContactStatus.BLACKLISTED]: 'Blacklisted',
  [ContactStatus.UNREACHABLE]: 'Unreachable',
  [ContactStatus.DO_NOT_CALL]: 'Do Not Call',
};

export const CONSENT_STATUS_LABELS: Record<ConsentStatus, string> = {
  [ConsentStatus.GRANTED]: 'Granted',
  [ConsentStatus.DENIED]: 'Denied',
  [ConsentStatus.PENDING]: 'Pending',
  [ConsentStatus.REVOKED]: 'Revoked',
  [ConsentStatus.EXPIRED]: 'Expired',
};

export const INDUSTRY_LABELS: Record<Industry, string> = {
  [Industry.HEALTHCARE]: 'Healthcare',
  [Industry.FINANCIAL]: 'Financial Services',
  [Industry.LEGAL]: 'Legal',
  [Industry.COLLECTIONS]: 'Collections',
  [Industry.SALES]: 'Sales',
  [Industry.CUSTOMER_SUPPORT]: 'Customer Support',
  [Industry.GENERAL]: 'General',
};

export const AGENT_TYPE_LABELS: Record<AgentType, string> = {
  [AgentType.INBOUND_SUPPORT]: 'Inbound Support',
  [AgentType.OUTBOUND_CAMPAIGN]: 'Outbound Campaign',
  [AgentType.APPOINTMENT_ASSISTANT]: 'Appointment Assistant',
  [AgentType.COLLECTIONS_AGENT]: 'Collections Agent',
  [AgentType.SALES_QUALIFIER]: 'Sales Qualifier',
  [AgentType.LEGAL_INTAKE]: 'Legal Intake',
  [AgentType.SURVEY_FEEDBACK]: 'Survey & Feedback',
};

export const WORKFLOW_NODE_TYPE_LABELS: Record<WorkflowNodeType, string> = {
  [WorkflowNodeType.GREETING]: 'Greeting',
  [WorkflowNodeType.CONSENT_CAPTURE]: 'Consent Capture',
  [WorkflowNodeType.VERIFICATION]: 'Verification',
  [WorkflowNodeType.QUALIFICATION]: 'Qualification',
  [WorkflowNodeType.INFO_CAPTURE]: 'Info Capture',
  [WorkflowNodeType.LOOKUP]: 'Data Lookup',
  [WorkflowNodeType.SUMMARIZE]: 'Summarize',
  [WorkflowNodeType.OFFER_OPTIONS]: 'Offer Options',
  [WorkflowNodeType.BOOK_APPOINTMENT]: 'Book Appointment',
  [WorkflowNodeType.COLLECT_PAYMENT]: 'Collect Payment',
  [WorkflowNodeType.ESCALATE_HUMAN]: 'Escalate to Human',
  [WorkflowNodeType.SEND_MESSAGE]: 'Send Message',
  [WorkflowNodeType.END_CALL]: 'End Call',
  [WorkflowNodeType.RETRY_LATER]: 'Retry Later',
  [WorkflowNodeType.MARK_UNREACHABLE]: 'Mark Unreachable',
  [WorkflowNodeType.DNC_OPTOUT]: 'DNC / Opt-Out',
  [WorkflowNodeType.DISPUTE]: 'Dispute',
  [WorkflowNodeType.COMPLAINT]: 'Complaint',
};

// ---------------------
// Terminal Call Statuses
// ---------------------

export const TERMINAL_CALL_STATUSES: CallStatus[] = [
  CallStatus.COMPLETED,
  CallStatus.FAILED,
  CallStatus.VOICEMAIL,
  CallStatus.NO_ANSWER,
  CallStatus.BUSY,
  CallStatus.CANCELLED,
];

export const RETRYABLE_CALL_STATUSES: CallStatus[] = [
  CallStatus.FAILED,
  CallStatus.NO_ANSWER,
  CallStatus.BUSY,
];

// ---------------------
// HTTP Status Codes
// ---------------------

export const HTTP_STATUS = {
  OK: 200,
  CREATED: 201,
  NO_CONTENT: 204,
  BAD_REQUEST: 400,
  UNAUTHORIZED: 401,
  FORBIDDEN: 403,
  NOT_FOUND: 404,
  CONFLICT: 409,
  UNPROCESSABLE_ENTITY: 422,
  TOO_MANY_REQUESTS: 429,
  INTERNAL_SERVER_ERROR: 500,
  SERVICE_UNAVAILABLE: 503,
} as const;

// ---------------------
// Regex Patterns
// ---------------------

export const PATTERNS = {
  E164_PHONE: /^\+[1-9]\d{1,14}$/,
  EMAIL: /^[^\s@]+@[^\s@]+\.[^\s@]+$/,
  SLUG: /^[a-z0-9]+(?:-[a-z0-9]+)*$/,
  UUID: /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
} as const;
