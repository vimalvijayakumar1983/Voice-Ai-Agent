// ---------- CRM Unified Types ----------

export enum CRMProvider {
  SALESFORCE = 'salesforce',
  HUBSPOT = 'hubspot',
}

export enum SyncDirection {
  INBOUND = 'inbound',
  OUTBOUND = 'outbound',
  BIDIRECTIONAL = 'bidirectional',
}

export enum SyncStatus {
  IDLE = 'idle',
  SYNCING = 'syncing',
  SUCCESS = 'success',
  FAILED = 'failed',
  PARTIAL = 'partial',
}

export enum ConflictResolution {
  CRM_WINS = 'crm_wins',
  VOICE_AI_WINS = 'voice_ai_wins',
  NEWEST_WINS = 'newest_wins',
}

// ---------- Entities ----------

export interface CRMContact {
  id?: string;
  externalId?: string;
  firstName: string;
  lastName: string;
  email?: string;
  phone?: string;
  company?: string;
  title?: string;
  source?: string;
  ownerId?: string;
  customFields?: Record<string, unknown>;
  createdAt?: string;
  updatedAt?: string;
}

export interface CRMDeal {
  id?: string;
  externalId?: string;
  name: string;
  amount?: number;
  currency?: string;
  stage: string;
  probability?: number;
  contactId?: string;
  accountId?: string;
  closeDate?: string;
  ownerId?: string;
  customFields?: Record<string, unknown>;
  createdAt?: string;
  updatedAt?: string;
}

export interface CRMAccount {
  id?: string;
  externalId?: string;
  name: string;
  website?: string;
  industry?: string;
  phone?: string;
  address?: CRMAddress;
  ownerId?: string;
  customFields?: Record<string, unknown>;
  createdAt?: string;
  updatedAt?: string;
}

export interface CRMAddress {
  street?: string;
  city?: string;
  state?: string;
  postalCode?: string;
  country?: string;
}

export interface CRMActivity {
  id?: string;
  externalId?: string;
  type: 'call' | 'email' | 'task' | 'meeting' | 'note';
  subject: string;
  description?: string;
  status?: string;
  contactId?: string;
  dealId?: string;
  duration?: number;
  outcome?: string;
  callDirection?: 'inbound' | 'outbound';
  recordingUrl?: string;
  scheduledAt?: string;
  completedAt?: string;
  customFields?: Record<string, unknown>;
}

// ---------- Field Mapping ----------

export interface FieldMapping {
  id?: string;
  sourceField: string;
  destinationField: string;
  direction: SyncDirection;
  transform?: FieldTransform;
  required?: boolean;
  defaultValue?: unknown;
}

export interface FieldTransform {
  type: FieldTransformType;
  config?: Record<string, unknown>;
}

export enum FieldTransformType {
  NONE = 'none',
  UPPERCASE = 'uppercase',
  LOWERCASE = 'lowercase',
  CAPITALIZE = 'capitalize',
  FORMAT_PHONE = 'format_phone',
  FORMAT_DATE = 'format_date',
  MAP_VALUES = 'map_values',
  CONCATENATE = 'concatenate',
  SPLIT = 'split',
  CUSTOM = 'custom',
}

export interface FieldMappingConfig {
  entityType: 'contact' | 'deal' | 'account' | 'activity';
  mappings: FieldMapping[];
  syncDirection: SyncDirection;
  conflictResolution: ConflictResolution;
}

// ---------- Sync Types ----------

export interface SyncResult {
  provider: CRMProvider;
  entityType: string;
  direction: SyncDirection;
  created: number;
  updated: number;
  deleted: number;
  failed: number;
  errors: SyncError[];
  startedAt: string;
  completedAt: string;
  durationMs: number;
}

export interface SyncError {
  entityId?: string;
  externalId?: string;
  operation: 'create' | 'update' | 'delete';
  message: string;
  details?: unknown;
}

export interface SyncOptions {
  fullSync?: boolean;
  since?: Date;
  entityTypes?: string[];
  batchSize?: number;
  dryRun?: boolean;
}

// ---------- OAuth Types ----------

export interface OAuthConfig {
  clientId: string;
  clientSecret: string;
  redirectUri: string;
  scopes: string[];
}

export interface OAuthTokens {
  accessToken: string;
  refreshToken?: string;
  expiresAt?: Date;
  tokenType: string;
  scope?: string;
  instanceUrl?: string;
}

// ---------- Provider Interface ----------

export interface ICRMProvider {
  readonly providerName: CRMProvider;

  // Authentication
  getAuthorizationUrl(state: string): string;
  exchangeCodeForTokens(code: string): Promise<OAuthTokens>;
  refreshAccessToken(refreshToken: string): Promise<OAuthTokens>;

  // Contacts
  getContacts(tokens: OAuthTokens, since?: Date, limit?: number, offset?: number): Promise<{ contacts: CRMContact[]; hasMore: boolean; total?: number }>;
  getContactById(tokens: OAuthTokens, externalId: string): Promise<CRMContact>;
  createContact(tokens: OAuthTokens, contact: CRMContact): Promise<CRMContact>;
  updateContact(tokens: OAuthTokens, externalId: string, contact: Partial<CRMContact>): Promise<CRMContact>;
  upsertContact(tokens: OAuthTokens, contact: CRMContact, matchField: string): Promise<CRMContact>;
  deleteContact(tokens: OAuthTokens, externalId: string): Promise<void>;

  // Deals
  getDeals(tokens: OAuthTokens, since?: Date, limit?: number, offset?: number): Promise<{ deals: CRMDeal[]; hasMore: boolean }>;
  createDeal(tokens: OAuthTokens, deal: CRMDeal): Promise<CRMDeal>;
  updateDeal(tokens: OAuthTokens, externalId: string, deal: Partial<CRMDeal>): Promise<CRMDeal>;

  // Activities
  logActivity(tokens: OAuthTokens, activity: CRMActivity): Promise<CRMActivity>;
  getActivities(tokens: OAuthTokens, contactId: string, since?: Date): Promise<CRMActivity[]>;

  // Accounts
  getAccounts(tokens: OAuthTokens, since?: Date, limit?: number, offset?: number): Promise<{ accounts: CRMAccount[]; hasMore: boolean }>;
  createAccount(tokens: OAuthTokens, account: CRMAccount): Promise<CRMAccount>;

  // Bulk operations
  bulkCreateContacts(tokens: OAuthTokens, contacts: CRMContact[]): Promise<{ successes: CRMContact[]; failures: SyncError[] }>;
  bulkUpdateContacts(tokens: OAuthTokens, contacts: Array<{ externalId: string; data: Partial<CRMContact> }>): Promise<{ successes: CRMContact[]; failures: SyncError[] }>;

  // Schema
  getAvailableFields(tokens: OAuthTokens, objectType: string): Promise<Array<{ name: string; label: string; type: string; required: boolean }>>;

  // Health
  testConnection(tokens: OAuthTokens): Promise<boolean>;
}
