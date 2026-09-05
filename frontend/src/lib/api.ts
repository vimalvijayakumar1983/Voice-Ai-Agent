import {
  canReplayAfterRefresh,
  sameSessionBoundary,
  type RefreshResult,
  type SessionBoundary,
} from './session-boundary.cjs';

// All browser requests use the same-origin Next.js proxy. Besides avoiding a
// needless cross-origin trust boundary, this keeps the rotating HttpOnly
// refresh cookie first-party on browsers that block third-party cookies.
const API_URL = '';
const SESSION_SIGNAL_KEY = 'vav:session-signal';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function validationIssueMessage(issue: unknown): string | null {
  if (typeof issue === 'string') return issue.trim() || null;
  if (!isRecord(issue)) return null;
  const message = typeof issue.msg === 'string'
    ? issue.msg.replace(/^Value error,\s*/i, '').trim()
    : '';
  const location = Array.isArray(issue.loc)
    ? issue.loc.filter((part) => part !== 'body').map(String).join('.')
    : '';
  if (!message) return null;
  return location ? `${location}: ${message}` : message;
}

export function formatApiError(payload: unknown, fallback: string): string {
  const detail = isRecord(payload) && 'detail' in payload ? payload.detail : payload;
  if (typeof detail === 'string') return detail.trim() || fallback;
  if (Array.isArray(detail)) {
    const messages = detail.map(validationIssueMessage).filter((value): value is string => Boolean(value));
    return messages.length ? messages.join(' ') : fallback;
  }
  if (isRecord(detail)) {
    if (typeof detail.message === 'string' && detail.message.trim()) return detail.message.trim();
    const issue = validationIssueMessage(detail);
    if (issue) return issue;
  }
  return fallback;
}

function createIdempotencyKey() {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  if (typeof globalThis.crypto?.getRandomValues === 'function') {
    const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  throw new Error('Secure idempotency-key generation is unavailable in this browser.');
}

export interface KnowledgeCompanyScope {
  semantic_retrieval_enabled?: boolean;
  default_company: string | null;
  companies: { name: string; aliases: string[] }[];
}

export interface VoiceAgent {
  knowledge_company_scope?: KnowledgeCompanyScope | null;
  id: string;
  name: string;
  description: string | null;
  is_active: boolean;
  system_prompt: string;
  model_provider: string;
  model_name: string;
  disposition_profile: 'general' | 'receptionist' | 'customer_support' | 'appointment' | 'sales' | 'collections';
  post_call_analysis_mode: 'provider_first' | 'vav_ai' | 'disabled';
  voice_provider: string;
  voice_id: string;
  language: string;
  supported_languages: string[];
  language_switching_enabled: boolean;
  language_switching_mode: 'disabled' | 'automatic';
  speech_rate: number;
  temperature: number;
  greeting_message: string | null;
  timezone: string;
  provider_agent_id: string | null;
  provider_branch_id: string | null;
  provider_revision_id: string | null;
  provider_config: {
    publish?: {
      phase?: string;
    };
  } | null;
  sync_status:
    | 'local_only'
    | 'dirty'
    | 'provisioning'
    | 'provision_unknown'
    | 'publishing'
    | 'provider_scanning'
    | 'publish_unknown'
    | 'synced'
    | 'error';
  last_synced_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentAIDraftRequest {
  brief: string;
  provider_preference: 'auto' | 'smallest' | 'sarvam' | 'elevenlabs' | 'inworld';
  primary_language: string;
  timezone: string;
}

export interface AgentAIDraftResponse {
  draft: {
    name: string;
    description: string | null;
    system_prompt: string;
    greeting_message: string | null;
    model_provider: string;
    model_name: string;
    disposition_profile: 'general' | 'receptionist' | 'customer_support' | 'appointment' | 'sales' | 'collections';
    post_call_analysis_mode: 'provider_first' | 'vav_ai' | 'disabled';
    voice_provider: 'smallest' | 'sarvam' | 'elevenlabs' | 'inworld';
    voice_id: string;
    temperature: number;
    language: string;
    supported_languages: string[];
    language_switching_enabled: boolean;
    language_switching_mode: 'disabled' | 'automatic';
    speech_rate: number;
    timezone: string;
  };
  rationale: string;
  assumptions: string[];
  recommended_knowledge_base_id: string | null;
  recommended_knowledge_base_name: string | null;
  model: string;
}

export interface ProviderStatus {
  provider: 'smallest';
  configured: boolean;
  webhook_configured: boolean;
  base_url: string;
  providers?: Record<'smallest' | 'sarvam' | 'elevenlabs' | 'inworld', {
    configured: boolean;
    agent_runtime: boolean;
    voice_preview: boolean;
    source?: 'workspace' | 'platform' | 'none';
    updated_at?: string | null;
  }>;
}

export interface ProviderCredentialStatus {
  provider: 'sarvam';
  configured: boolean;
  source: 'workspace' | 'platform' | 'none';
  updated_at: string | null;
}

export type WorkspaceProviderName = 'smallest' | 'sarvam' | 'elevenlabs' | 'inworld' | 'openai' | 'twilio';

export interface WorkspaceCredentialStatus {
  provider: WorkspaceProviderName;
  configured: boolean;
  source: 'workspace' | 'platform' | 'none';
  updated_at: string | null;
  account_sid_hint: string | null;
  default_from_number: string | null;
}

export interface WorkspaceCredentialStatuses {
  providers: Record<WorkspaceProviderName, WorkspaceCredentialStatus>;
}

export interface RuntimeProfile {
  id: string | null;
  agent_id: string;
  enabled: boolean;
  telephony_provider: 'twilio' | 'livekit_sip';
  primary_speech_provider: 'sarvam' | 'elevenlabs' | 'inworld';
  fallback_speech_provider: 'smallest' | 'sarvam' | 'elevenlabs' | 'inworld' | null;
  llm_provider: 'openai' | 'inworld';
  llm_model: string;
  voice_runtime: 'pipeline' | 'inworld_realtime';
  knowledge_turn_mode: 'tool_loop' | 'single_pass_experimental';
  stt_language: string;
  stt_model: 'auto' | 'assemblyai/u3-rt-pro' | 'soniox/stt-rt-v4' | 'inworld/inworld-stt-1';
  tts_delivery_mode: 'stable' | 'balanced' | 'creative';
  inworld_realtime_tts_model: 'inworld-tts-1.5-max' | 'inworld-tts-1.5-mini' | 'inworld-tts-2';
  diagnostic_recording_mode: 'off' | 'livekit_egress_explicit_consent';
  max_concurrent_calls: number;
  daily_call_limit: number;
  monthly_budget_cents: number;
  assigned_numbers: string[];
  status: 'draft' | 'ready' | 'blocked' | 'active' | 'inactive';
  ready: boolean;
  blockers: string[];
  last_tested_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface RuntimeReadiness {
  agent_id: string;
  ready: boolean;
  status: 'ready' | 'blocked';
  blockers: string[];
  checks: Record<string, boolean>;
  tested_at: string;
}

export interface SipCredentialStatus {
  configured: boolean;
  route_recorded: boolean;
  gateway_provisioned: boolean;
  inbound_trunk_hint: string | null;
  dispatch_rule_hint: string | null;
  agent_name: string | null;
  updated_at: string | null;
}

export interface VoiceCatalogItem {
  provider: 'smallest' | 'sarvam' | 'elevenlabs' | 'inworld';
  id: string;
  name: string;
  languages: string[];
  accent: string | null;
  gender: string | null;
  age: string | null;
  use_cases: string[];
  synthesizer_model: string | null;
  voice_pool: 'standard' | 'pro' | 'cloned' | 'unknown';
  unavailability_reason: string | null;
  source: 'catalog' | 'cloned';
}

export interface VoiceClone {
  id: string;
  provider: 'smallest';
  provider_voice_id: string | null;
  display_name: string;
  description: string | null;
  language: string;
  accent: string | null;
  gender: 'female' | 'male' | null;
  model: 'lightning-v3.1' | 'lightning-v3.1-pro';
  model_ids: string[];
  status: 'creating' | 'pending' | 'processing' | 'completed' | 'creation_unknown' | 'error' | 'missing' | 'deletion_unknown' | 'delete_error';
  last_error: string | null;
  last_synced_at: string | null;
  consent_confirmed_at: string;
  created_at: string;
  updated_at: string;
}

export interface LanguageCatalogItem {
  code: string;
  name: string;
}

export interface AgentTemplate {
  id: string;
  name: string;
  category: string;
  description: string;
  system_prompt: string;
  greeting_message: string;
  default_language: string;
  supported_languages: string[];
  voice_id: string;
  speech_rate: number;
  temperature: number;
  timezone: string;
  disposition_profile: 'general' | 'receptionist' | 'customer_support' | 'appointment' | 'sales' | 'collections';
  post_call_analysis_mode: 'provider_first' | 'vav_ai' | 'disabled';
}

export interface AgentProviderCatalog {
  provider: 'multi';
  voice_model: string;
  voices: VoiceCatalogItem[];
  languages: LanguageCatalogItem[];
  templates: AgentTemplate[];
}

export type KnowledgeScope = 'workspace' | 'group' | 'division' | 'branch' | 'department';
export type KnowledgeSyncStatus = 'local_only' | 'provisioning' | 'processing' | 'ready' | 'error';
export type KnowledgeProcessingMode = 'automatic' | 'fast' | 'ai_verified';

export interface KnowledgeSource {
  id: string;
  knowledge_base_id: string;
  source_type: 'website' | 'sitemap' | 'url' | 'file' | 'text';
  name: string;
  location: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  status: 'pending' | 'processing' | 'indexed' | 'failed' | 'local_only';
  provider_item_id: string | null;
  error_message: string | null;
  source_metadata: Record<string, unknown> | null;
  retrieval_ready: boolean;
  extracted_character_count: number;
  last_synced_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeAgentBinding {
  id: string;
  agent_id: string;
  agent_name: string;
  knowledge_base_id: string;
  sync_status: string;
  last_synced_at: string | null;
}

export interface KnowledgeCrawlPage {
  id: string;
  knowledge_source_id: string | null;
  url: string;
  canonical_url: string;
  depth: number;
  discovered_via: string;
  status: 'discovered' | 'queued' | 'processing' | 'indexed' | 'failed' | 'skipped';
  error_code: string | null;
  error_message: string | null;
  retry_count: number;
  last_attempted_at: string | null;
}

export interface KnowledgeCrawl {
  id: string;
  knowledge_base_id: string;
  root_url: string;
  allowed_host: string;
  status: 'queued' | 'discovering' | 'indexing' | 'retrying' | 'completed' | 'completed_with_errors' | 'failed' | 'cancelled';
  max_pages: number;
  max_depth: number;
  include_subdomains: boolean;
  discovered_count: number;
  queued_count: number;
  indexed_count: number;
  failed_count: number;
  skipped_count: number;
  options: Record<string, unknown> | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  pages: KnowledgeCrawlPage[];
  created_at: string;
  updated_at: string;
}

export interface KnowledgeServingRevision {
  revision_id: string;
  compiler_version: string;
  source_revision_sha256: string;
  chunk_revision_sha256: string;
  fact_revision_sha256: string;
  entity_revision_sha256: string;
  content_sha256: string;
  published_at: string;
  source_count: number;
  chunk_count: number;
  fact_count: number;
  entity_count: number;
  speech_lexicon_artifact_id: string;
}

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string | null;
  provider: string;
  provider_knowledge_base_id: string | null;
  sync_status: KnowledgeSyncStatus;
  sync_error: string | null;
  approval_status: 'draft' | 'approved';
  scope_type: KnowledgeScope;
  scope_label: string | null;
  languages: string[];
  tags: string[];
  source_count: number;
  indexed_source_count: number;
  last_synced_at: string | null;
  published_at: string | null;
  speech_lexicon: {
    artifact_id: string;
    compiler_version: string;
    source_revision_sha256: string;
    content_sha256: string;
    generated_at: string;
    source_count: number;
    entry_count: number;
    coverage: Record<string, number>;
  } | null;
  serving_revision: KnowledgeServingRevision | null;
  has_pending_changes: boolean;
  sources: KnowledgeSource[];
  agent_bindings: KnowledgeAgentBinding[];
  crawls: KnowledgeCrawl[];
  created_at: string;
  updated_at: string;
}

export interface KnowledgeAIDraftRequest {
  brief: string;
  scope_preference: 'auto' | KnowledgeScope;
  languages: string[];
}

export interface KnowledgeAIDraftResponse {
  draft: {
    name: string;
    description: string;
    scope_type: KnowledgeScope;
    scope_label: string | null;
    languages: string[];
    tags: string[];
  };
  rationale: string;
  assumptions: string[];
  recommended_sources: string[];
  model: string;
}

export interface BrowserSession {
  access_token: string;
  expires_in: number;
  sample_rate: number;
}

/**
 * Ephemeral, backend-issued LiveKit browser credential.
 *
 * The frontend requests this only after microphone permission succeeds and
 * keeps the access token in memory for the lifetime of the connection. The
 * LiveKit API key and secret never cross the server boundary.
 */
export interface LiveKitBrowserSession {
  access_token: string;
  url: string;
  room_name: string;
  participant_identity: string;
  call_id: string;
  expires_in: number;
  max_duration_seconds: number;
}

export type WorkspaceRole = 'owner' | 'admin' | 'member' | 'viewer';

export interface CurrentUser {
  id: string;
  email: string;
  full_name: string;
  role: WorkspaceRole;
  is_active: boolean;
  tenant_id: string;
  tenant_name: string | null;
  tenant_slug: string | null;
}

export interface WorkspaceInvitation {
  id: string;
  tenant_id: string;
  invited_by_user_id: string | null;
  email: string;
  full_name: string;
  role: Exclude<WorkspaceRole, 'owner'>;
  status: 'pending' | 'accepted' | 'expired' | 'revoked';
  expires_at: string;
  accepted_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface CreatedWorkspaceInvitation extends WorkspaceInvitation {
  token: string;
}

export interface WorkspaceApiKey {
  id: string;
  name: string;
  is_active: boolean;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
}

export interface CreatedWorkspaceApiKey extends WorkspaceApiKey {
  key: string;
}

export interface AuditEvent {
  id: string;
  tenant_id: string;
  actor_user_id: string | null;
  action: string;
  resource_type: string;
  resource_id: string | null;
  details: Record<string, unknown>;
  ip_address: string | null;
  user_agent: string | null;
  created_at: string;
}

interface AuthTokens {
  access_token: string;
  tenant_id: string;
  user_id: string;
  role: string;
}

export interface RegistrationPolicy {
  mode: 'bootstrap' | 'invite_only' | 'open';
  registration_available: boolean;
  message: string;
}

export interface CallRecord {
  id: string;
  tenant_id: string;
  agent_id: string | null;
  campaign_id: string | null;
  direction: 'inbound' | 'outbound';
  status: string;
  from_number: string;
  to_number: string;
  provider: string;
  provider_call_sid: string | null;
  recording_available: boolean;
  call_metadata?: Record<string, unknown> | null;
  started_at: string | null;
  answered_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  cost_cents: number | null;
  disposition: string | null;
  sentiment_score: number | null;
  created_at: string;
}

export interface AnalyticsOverview {
  total_calls: number;
  total_minutes: number;
  avg_duration_seconds: number;
  total_cost_cents: number;
  calls_by_status: Record<string, number>;
  calls_by_direction: Record<string, number>;
  calls_by_disposition: Record<string, number>;
}

export interface AnalyticsTimeSeries {
  period: string;
  data: Array<{ date: string; calls: number; minutes: number }>;
}

export interface AgentPerformance {
  agent_id: string;
  agent_name: string;
  total_calls: number;
  avg_duration_seconds: number;
  success_rate: number;
  avg_sentiment: number | null;
}

export interface CallTranscript {
  id: string;
  call_id: string;
  turns: Array<Record<string, unknown>>;
  full_text: string | null;
}

export interface CallSummary {
  id: string;
  call_id: string;
  summary: string;
  key_topics: string[] | null;
  action_items: string[] | null;
  sentiment: string | null;
  disposition_details: {
    version: string;
    profile: string;
    primary: string;
    secondary: string | null;
    resolution: string;
    customer_intent: string | null;
    follow_up: {
      required: boolean;
      action: string | null;
      owner: string | null;
      due_at: string | null;
    };
    confidence: number;
    evidence: string[];
    needs_review: boolean;
  } | null;
}

export interface ProviderHistorySyncResult {
  scanned: number;
  imported: number;
  updated: number;
  failed: number;
}

export interface Campaign {
  id: string;
  tenant_id: string;
  agent_id: string | null;
  workflow_id: string | null;
  name: string;
  description: string | null;
  status: string;
  campaign_type: string;
  scheduled_start: string | null;
  scheduled_end: string | null;
  calling_hours_start: string | null;
  calling_hours_end: string | null;
  timezone: string;
  max_concurrent_calls: number;
  retry_attempts: number;
  total_contacts: number;
  completed_contacts: number;
  successful_contacts: number;
  created_at: string;
}

export interface CampaignContactInput {
  phone_number: string;
  name?: string;
  context_data?: Record<string, string | number | boolean>;
}

export interface CampaignAttempt {
  id: string;
  campaign_id: string;
  contact_id: string;
  call_id: string | null;
  attempt_number: number;
  provider: string;
  provider_call_sid: string | null;
  state: string;
  dispatch_started_at: string | null;
  accepted_at: string | null;
  finished_at: string | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
}

export interface CampaignAttemptReconciliation {
  action: 'release_for_retry';
  reason: string;
}

export interface CampaignCreateRequest {
  name: string;
  agent_id: string;
  timezone: string;
  calling_hours_start: string;
  calling_hours_end: string;
  scheduled_start: string | null;
  scheduled_end: string | null;
  max_concurrent_calls: number;
  retry_attempts: number;
  contacts: CampaignContactInput[];
}

export type WorkflowTrigger = 'inbound_call' | 'campaign' | 'api';

export type WorkflowNodeType =
  | 'greeting'
  | 'gather_input'
  | 'ai_conversation'
  | 'transfer'
  | 'hangup'
  | 'condition'
  | 'webhook';

export interface WorkflowNodeInput {
  position: number;
  node_type: WorkflowNodeType;
  config: Record<string, unknown>;
  next_node_id?: string | null;
}

export interface WorkflowNode extends Omit<WorkflowNodeInput, 'next_node_id'> {
  id: string;
  next_node_id: string | null;
}

export interface CallWorkflow {
  id: string;
  tenant_id: string;
  agent_id: string | null;
  name: string;
  description: string | null;
  is_active: boolean;
  trigger_type: WorkflowTrigger;
  config: Record<string, unknown> | null;
  nodes: WorkflowNode[];
}

export interface WorkflowCreateRequest {
  name: string;
  trigger_type: WorkflowTrigger;
  agent_id?: string | null;
  description?: string | null;
  config?: Record<string, unknown> | null;
  nodes?: WorkflowNodeInput[];
  is_active?: boolean;
}

export interface WorkflowUpdateRequest {
  name?: string;
  trigger_type?: WorkflowTrigger;
  agent_id?: string | null;
  description?: string | null;
  config?: Record<string, unknown> | null;
  nodes?: WorkflowNodeInput[];
  is_active?: boolean;
}

export interface Integration {
  id: string;
  tenant_id: string;
  name: string;
  integration_type: string;
  config: Record<string, unknown>;
  secret_fields: string[];
  is_active: boolean;
}

export type IntegrationType = 'webhook' | 'his_api' | 'vav_crm' | 'google_sheets';

export interface IntegrationCreateRequest {
  name: string;
  integration_type: IntegrationType;
  config: Record<string, unknown>;
}

export interface IntegrationUpdateRequest {
  name?: string;
  config?: Record<string, unknown>;
  is_active?: boolean;
  clear_secrets?: string[];
}

export interface IntegrationDelivery {
  id: string;
  integration_id: string;
  event_type: string;
  status: 'pending' | 'sent' | 'failed';
  attempts: number;
  last_error: string | null;
  delivered_at: string | null;
  created_at: string;
}

export interface DncEntry {
  id: string;
  phone_number: string;
  reason: string | null;
  source: string | null;
}

export type ConsentType =
  | 'outbound_call'
  | 'marketing_call'
  | 'recording'
  | 'data_processing';

export interface ConsentRecord {
  id: string;
  phone_number: string;
  consent_type: ConsentType;
  status: 'granted' | 'revoked';
  evidence: Record<string, unknown> | null;
  granted_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface UsageSummary {
  period_start: string;
  period_end: string;
  total_minutes: number;
  total_ai_tokens: number;
  total_cost_cents: number;
  included_minutes: number;
  overage_minutes: number;
  overage_cost_cents: number;
}

export interface BillingPlan {
  id: string;
  name: string;
  base_price_cents: number;
  included_minutes: number;
  per_minute_cents: number;
  max_agents: number;
  max_concurrent_calls: number;
  features: Record<string, unknown> | null;
}

export interface CostReportFilters {
  days?: number;
  provider?: 'twilio' | 'smallest' | 'livekit_sip' | 'livekit_webrtc' | '';
  speech_provider?: 'inworld' | 'sarvam' | 'elevenlabs' | 'smallest' | '';
  agent_id?: string;
  direction?: 'inbound' | 'outbound' | '';
  status?: string;
}

export interface CostComponent {
  provider: string;
  service: string;
  quantity: number;
  unit: string;
  rate_usd: number;
  cost_usd: number;
  cost_aed: number;
  source_url: string;
  basis: string;
}

export interface CostCallRow {
  call_id: string;
  created_at: string;
  agent_id: string | null;
  agent_name: string;
  direction: string;
  status: string;
  disposition: string | null;
  telephony_provider: string;
  speech_provider: string | null;
  from_number: string;
  to_number: string;
  duration_seconds: number;
  cost_usd: number;
  cost_aed: number;
  ledger_cost_usd: number;
  ledger_cost_aed: number;
  cost_state: string;
  pricing_completeness: string;
  missing_cost_inputs: string[];
  components: CostComponent[];
}

export interface ProviderRateCard {
  provider: string;
  service: string;
  native_amount: number;
  native_currency: string;
  unit: string;
  usd: number;
  aed: number;
  source_url: string;
  effective_date: string;
  notes: string;
}

export interface CostReport {
  period_start: string;
  period_end: string;
  currency: {
    display: string[];
    usd_to_aed: number;
    inr_to_aed: number;
    fx_effective_date: string;
    source_url: string;
    notes: string;
  };
  summary: {
    total_calls: number;
    answered_calls: number;
    completed_calls: number;
    successful_calls: number;
    total_minutes: number;
    avg_duration_seconds: number;
    answer_rate: number;
    success_rate: number;
    estimated_cost_usd: number;
    estimated_cost_aed: number;
    avg_cost_per_call_usd: number;
    avg_cost_per_call_aed: number;
    cost_per_minute_usd: number;
    cost_per_minute_aed: number;
    priced_calls: number;
    fully_priced_calls: number;
    unpriced_calls: number;
    cost_coverage: number;
    full_cost_coverage: number;
    ledger_estimate_usd: number;
    ledger_estimate_aed: number;
    calls_by_status: Record<string, number>;
    calls_by_direction: Record<string, number>;
  };
  provider_breakdown: Array<{
    provider: string;
    service: string;
    calls: number;
    quantity: number;
    unit: string;
    cost_usd: number;
    cost_aed: number;
    source_url: string;
    basis: string;
  }>;
  trend: Array<{ date: string; calls: number; minutes: number; cost_usd: number; cost_aed: number }>;
  calls: CostCallRow[];
  rate_cards: ProviderRateCard[];
  methodology: { primary_total: string; not_included: string; invoice_status: string };
}

class ApiClient {
  private token: string | null = null;
  private refreshPromise: Promise<RefreshResult> | null = null;
  private refreshPromiseBoundary: SessionBoundary | null = null;
  private currentUserPromise: Promise<CurrentUser> | null = null;
  private sessionEpoch = 0;
  private sessionSyncInstalled = false;
  private sessionReloadScheduled = false;
  private sessionChannel: BroadcastChannel | null = null;
  private legacyRefreshToken: string | null = null;
  private readonly refreshLeaseOwner = typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random()}`;

  private async waitForRefreshLeaseChange(delayMs: number) {
    await new Promise<void>((resolve) => {
      if (typeof window === 'undefined') {
        resolve();
        return;
      }
      let timer = 0;
      const finish = () => {
        window.clearTimeout(timer);
        window.removeEventListener('storage', handleStorage);
        resolve();
      };
      const handleStorage = (event: StorageEvent) => {
        if (event.storageArea === window.localStorage && event.key === 'vav:refresh-lease') {
          finish();
        }
      };
      timer = window.setTimeout(finish, delayMs);
      window.addEventListener('storage', handleStorage);
    });
  }

  private async withRefreshLease<T>(action: () => Promise<T>): Promise<T> {
    if (typeof window === 'undefined') return action();

    type LockManagerLike = {
      request: <Value>(name: string, callback: () => Promise<Value>) => Promise<Value>;
    };
    const lockManager = (navigator as Navigator & { locks?: LockManagerLike }).locks;
    if (lockManager) {
      return lockManager.request('vav:refresh-session', action);
    }

    // Web Locks are preferred. This expiring localStorage lease is a fallback
    // for older browsers; the backend also has a short non-destructive race
    // window so a rare simultaneous claim cannot revoke a successful refresh.
    const leaseKey = 'vav:refresh-lease';
    const leaseLifetimeMs = 6_000;
    let leaseAcquired = false;
    try {
      while (true) {
        const now = Date.now();
        let lease: { owner?: string; expiresAt?: number } = {};
        try {
          lease = JSON.parse(window.localStorage.getItem(leaseKey) || '{}') as typeof lease;
        } catch {
          lease = {};
        }
        if (!lease.owner || !lease.expiresAt || lease.expiresAt <= now) {
          const candidate = JSON.stringify({
            owner: this.refreshLeaseOwner,
            expiresAt: now + leaseLifetimeMs,
          });
          window.localStorage.setItem(leaseKey, candidate);
          if (window.localStorage.getItem(leaseKey) === candidate) {
            leaseAcquired = true;
            break;
          }
        }
        await this.waitForRefreshLeaseChange(75 + Math.floor(Math.random() * 75));
      }
    } catch {
      // Storage can be unavailable in hardened browser modes. Refresh remains
      // one-time server-side, with the race grace preventing family teardown.
      return action();
    }

    try {
      return await action();
    } finally {
      if (leaseAcquired) {
        try {
          const lease = JSON.parse(
            window.localStorage.getItem(leaseKey) || '{}',
          ) as { owner?: string };
          if (lease.owner === this.refreshLeaseOwner) {
            window.localStorage.removeItem(leaseKey);
          }
        } catch {
          // Best-effort release; the lease has a short crash-safe expiry.
        }
      }
    }
  }

  private ensureSessionSync() {
    if (typeof window === 'undefined' || this.sessionSyncInstalled) return;
    this.sessionSyncInstalled = true;

    // Remove credentials written by releases before the HttpOnly-cookie
    // migration. New credentials are never persisted in browser-readable
    // storage; these removals are cleanup only.
    try {
      this.legacyRefreshToken = window.localStorage.getItem('refresh_token');
      window.localStorage.removeItem('access_token');
      window.localStorage.removeItem('refresh_token');
    } catch {
      // Storage may be disabled. The in-memory session still works.
    }

    const handleSignal = (value: unknown) => {
      const type = typeof value === 'object' && value !== null
        ? (value as { type?: unknown }).type
        : undefined;
      if (type !== 'changed' && type !== 'logout') return;

      // A different tab changed the shared HttpOnly cookie. Invalidate every
      // in-flight boundary before clearing local page identity.
      this.beginSessionMutation();
      this.token = null;
      this.legacyRefreshToken = null;
      if (type === 'logout') {
        window.dispatchEvent(new Event('vav:auth-expired'));
        return;
      }
      if (!this.sessionReloadScheduled) {
        this.sessionReloadScheduled = true;
        window.dispatchEvent(new Event('vav:session-changed'));
        window.setTimeout(() => window.location.reload(), 0);
      }
    };

    window.addEventListener('storage', (event) => {
      if (event.storageArea !== window.localStorage || event.key !== SESSION_SIGNAL_KEY) return;
      try {
        handleSignal(JSON.parse(event.newValue || '{}'));
      } catch {
        // Ignore malformed non-credential coordination messages.
      }
    });

    if (typeof BroadcastChannel !== 'undefined') {
      this.sessionChannel = new BroadcastChannel('vav:auth-session');
      this.sessionChannel.addEventListener('message', (event) => handleSignal(event.data));
    }
  }

  private publishSessionSignal(type: 'changed' | 'logout') {
    if (typeof window === 'undefined') return;
    this.ensureSessionSync();
    const signal = {
      type,
      id: typeof crypto.randomUUID === 'function'
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random()}`,
    };
    this.sessionChannel?.postMessage(signal);
    try {
      window.localStorage.setItem(SESSION_SIGNAL_KEY, JSON.stringify(signal));
    } catch {
      // BroadcastChannel remains the primary storage-free coordination path.
    }
  }

  private beginSessionMutation() {
    // A login, registration, invitation acceptance, or logout supersedes any
    // refresh already in flight. The request itself cannot be cancelled on all
    // supported browsers, so its response is guarded by this generation value.
    this.sessionEpoch += 1;
    this.refreshPromise = null;
    this.refreshPromiseBoundary = null;
    this.currentUserPromise = null;
    return this.sessionEpoch;
  }

  private commitSession(data: AuthTokens, expectedEpoch: number, announceChange = false) {
    if (this.sessionEpoch !== expectedEpoch) return false;

    this.sessionEpoch += 1;
    this.refreshPromise = null;
    this.currentUserPromise = null;
    this.token = data.access_token;
    if (typeof window !== 'undefined') this.ensureSessionSync();
    if (announceChange && typeof window !== 'undefined') {
      window.dispatchEvent(new Event('vav:session-committed'));
      this.publishSessionSignal('changed');
    }
    return true;
  }

  getToken(): string | null {
    if (typeof window !== 'undefined') this.ensureSessionSync();
    return this.token;
  }

  private readSessionBoundary(): SessionBoundary {
    return { epoch: this.sessionEpoch, accessToken: this.token };
  }

  private clearSession() {
    this.beginSessionMutation();
    this.token = null;
    this.legacyRefreshToken = null;
    if (typeof window !== 'undefined') this.ensureSessionSync();
  }

  private notifyAuthExpired(announce = true) {
    this.clearSession();
    if (typeof window !== 'undefined') {
      if (announce) this.publishSessionSignal('logout');
      window.dispatchEvent(new Event('vav:auth-expired'));
    }
  }

  private async refreshAccessToken(
    expectedBoundary: SessionBoundary,
  ): Promise<RefreshResult> {
    this.ensureSessionSync();
    if (this.refreshPromise) {
      if (
        this.refreshPromiseBoundary
        && sameSessionBoundary(this.refreshPromiseBoundary, expectedBoundary)
      ) {
        return this.refreshPromise;
      }
      return { status: 'session_changed' };
    }

    const attempt = this.withRefreshLease(async () => {
      if (!sameSessionBoundary(this.readSessionBoundary(), expectedBoundary)) {
        return { status: 'session_changed' } as RefreshResult;
      }

      const legacyRefreshToken = this.legacyRefreshToken;
      this.legacyRefreshToken = null;
      const response = await fetch(
        `${API_URL}/api/v1/auth/${legacyRefreshToken ? 'migrate-session' : 'refresh'}`,
        {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          ...(legacyRefreshToken
            ? { body: JSON.stringify({ refresh_token: legacyRefreshToken }) }
            : {}),
        },
      );
      if (!response.ok) {
        // A fallback-lease race returns 409 without revoking the winner. Give
        // its storage event a moment to publish the rotated pair. The loser
        // still cannot prove the successor belongs to its source session, so
        // it aborts the original request instead of adopting and replaying.
        if (response.status === 409) {
          await this.waitForRefreshLeaseChange(250);
          return { status: 'session_changed' } as RefreshResult;
        }
        return sameSessionBoundary(this.readSessionBoundary(), expectedBoundary)
          ? { status: 'failed' } as RefreshResult
          : { status: 'session_changed' } as RefreshResult;
      }
      const data = await response.json() as AuthTokens;
      if (
        !sameSessionBoundary(this.readSessionBoundary(), expectedBoundary)
        || !this.commitSession(data, expectedBoundary.epoch)
      ) {
        return { status: 'session_changed' } as RefreshResult;
      }
      return {
        status: 'rotated',
        source: expectedBoundary,
        result: {
          epoch: this.sessionEpoch,
          accessToken: data.access_token,
        },
      } as RefreshResult;
    }).catch(() => (
      sameSessionBoundary(this.readSessionBoundary(), expectedBoundary)
        ? { status: 'failed' } as RefreshResult
        : { status: 'session_changed' } as RefreshResult
    ));

    this.refreshPromise = attempt;
    this.refreshPromiseBoundary = expectedBoundary;
    try {
      return await attempt;
    } finally {
      // Do not clear a newer deduplicated attempt that started after logout or login.
      if (this.refreshPromise === attempt) {
        this.refreshPromise = null;
        this.refreshPromiseBoundary = null;
      }
    }
  }

  private async request<T>(
    path: string,
    options: RequestInit = {},
    authenticated = true,
    allowRefresh = true,
    expectedBoundary: SessionBoundary | null = null,
  ): Promise<T> {
    if (authenticated) this.ensureSessionSync();
    const requestBoundary = this.readSessionBoundary();
    if (expectedBoundary && !sameSessionBoundary(requestBoundary, expectedBoundary)) {
      throw new Error('The browser session changed while this request was running.');
    }
    const token = authenticated ? requestBoundary.accessToken : null;
    const headers: Record<string, string> = {
      ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...(options.headers as Record<string, string> || {}),
    };
    if (authenticated && token) {
      headers['Authorization'] = `Bearer ${token}`;
    }

    const response = await fetch(`${API_URL}${path}`, {
      ...options,
      credentials: 'include',
      headers,
    });

    if (response.status === 401 && authenticated) {
      // A request body can be replayed only after this tab proves it rotated
      // the exact access/refresh pair and epoch that authorized the first try.
      // Any cross-tab replacement is a safe UX retry, never an automatic write
      // under a potentially unrelated tenant.
      if (!sameSessionBoundary(this.readSessionBoundary(), requestBoundary)) {
        throw new Error('The browser session changed while this request was running.');
      }
      const refreshResult = allowRefresh
        ? await this.refreshAccessToken(requestBoundary)
        : { status: 'failed' } as RefreshResult;
      const currentBoundary = this.readSessionBoundary();
      if (
        refreshResult.status === 'rotated'
        && canReplayAfterRefresh(requestBoundary, refreshResult, currentBoundary)
      ) {
        return this.request<T>(path, options, true, false, refreshResult.result);
      }
      if (
        refreshResult.status === 'session_changed'
        || !sameSessionBoundary(currentBoundary, requestBoundary)
      ) {
        throw new Error('The browser session changed while this request was running.');
      }
      this.notifyAuthExpired();
      throw new Error('Your session has expired. Please sign in again.');
    }

    if (!response.ok) {
      const error: unknown = await response.json().catch(() => null);
      throw new Error(formatApiError(error, 'Request failed'));
    }

    if (response.status === 204) return undefined as T;
    return response.json();
  }

  private async requestBlob(
    path: string,
    options: RequestInit = {},
    allowRefresh = true,
    expectedBoundary: SessionBoundary | null = null,
  ): Promise<Blob> {
    this.ensureSessionSync();
    const requestBoundary = this.readSessionBoundary();
    if (expectedBoundary && !sameSessionBoundary(requestBoundary, expectedBoundary)) {
      throw new Error('The browser session changed while this request was running.');
    }
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...(options.headers as Record<string, string> || {}),
    };
    if (requestBoundary.accessToken) {
      headers.Authorization = `Bearer ${requestBoundary.accessToken}`;
    }

    const response = await fetch(`${API_URL}${path}`, {
      ...options,
      credentials: 'include',
      headers,
    });
    if (response.status === 401) {
      if (!sameSessionBoundary(this.readSessionBoundary(), requestBoundary)) {
        throw new Error('The browser session changed while this request was running.');
      }
      const refreshResult = allowRefresh
        ? await this.refreshAccessToken(requestBoundary)
        : { status: 'failed' } as RefreshResult;
      const currentBoundary = this.readSessionBoundary();
      if (
        refreshResult.status === 'rotated'
        && canReplayAfterRefresh(requestBoundary, refreshResult, currentBoundary)
      ) {
        return this.requestBlob(path, options, false, refreshResult.result);
      }
      if (
        refreshResult.status === 'session_changed'
        || !sameSessionBoundary(currentBoundary, requestBoundary)
      ) {
        throw new Error('The browser session changed while this request was running.');
      }
      this.notifyAuthExpired();
      throw new Error('Your session has expired. Please sign in again.');
    }
    if (!response.ok) {
      const error: unknown = await response.json().catch(() => null);
      throw new Error(formatApiError(error, 'Audio request failed'));
    }
    return response.blob();
  }

  // Auth
  async login(email: string, password: string) {
    const loginEpoch = this.beginSessionMutation();
    const data = await this.request<AuthTokens>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }, false);
    if (!this.commitSession(data, loginEpoch, true)) {
      throw new Error('A newer session action replaced this sign-in attempt.');
    }
    return data;
  }

  getRegistrationPolicy() {
    return this.request<RegistrationPolicy>('/api/v1/auth/registration-policy', {}, false);
  }

  async register(data: {
    email: string;
    password: string;
    full_name: string;
    tenant_name: string;
    tenant_slug: string;
  }) {
    const registrationEpoch = this.beginSessionMutation();
    const result = await this.request<AuthTokens>('/api/v1/auth/register', {
      method: 'POST',
      body: JSON.stringify(data),
    }, false);
    if (!this.commitSession(result, registrationEpoch, true)) {
      throw new Error('A newer session action replaced this registration attempt.');
    }
    return result;
  }

  getMe() {
    if (!this.currentUserPromise) {
      this.currentUserPromise = this.request<CurrentUser>('/api/v1/auth/me')
        .catch((error) => {
          this.currentUserPromise = null;
          throw error;
        });
    }
    return this.currentUserPromise;
  }

  logout() {
    // Clear the in-memory credential first. Remote revocation is deliberately
    // best-effort so logout remains safe during an API outage or after the
    // short-lived access token has already expired.
    this.notifyAuthExpired();
    void fetch(`${API_URL}/api/v1/auth/logout`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      keepalive: true,
    }).catch(() => undefined);
  }

  async acceptInvitation(token: string, password: string) {
    const invitationEpoch = this.beginSessionMutation();
    const data = await this.request<AuthTokens>('/api/v1/auth/invitations/accept', {
      method: 'POST',
      body: JSON.stringify({ token, password }),
    }, false);
    if (!this.commitSession(data, invitationEpoch, true)) {
      throw new Error('A newer session action replaced this invitation acceptance.');
    }
    return data;
  }

  async listWorkspaceUsers() {
    return this.request<CurrentUser[]>('/api/v1/auth/users');
  }

  async updateWorkspaceUser(
    userId: string,
    data: { role?: Exclude<WorkspaceRole, 'owner'>; is_active?: boolean },
  ) {
    return this.request<CurrentUser>(`/api/v1/auth/users/${userId}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  }

  async createInvitation(data: {
    email: string;
    full_name: string;
    role: Exclude<WorkspaceRole, 'owner'>;
  }) {
    return this.request<CreatedWorkspaceInvitation>('/api/v1/auth/invite', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async listInvitations() {
    return this.request<WorkspaceInvitation[]>('/api/v1/auth/invitations');
  }

  async revokeInvitation(invitationId: string) {
    return this.request<void>(`/api/v1/auth/invitations/${invitationId}`, {
      method: 'DELETE',
    });
  }

  async createApiKey(name: string) {
    return this.request<CreatedWorkspaceApiKey>('/api/v1/auth/api-keys', {
      method: 'POST',
      body: JSON.stringify({ name }),
    });
  }

  async listApiKeys() {
    return this.request<WorkspaceApiKey[]>('/api/v1/auth/api-keys');
  }

  async revokeApiKey(apiKeyId: string) {
    return this.request<void>(`/api/v1/auth/api-keys/${apiKeyId}`, {
      method: 'DELETE',
    });
  }

  async listAuditEvents(limit = 20) {
    return this.request<AuditEvent[]>(`/api/v1/audit-events?limit=${limit}`);
  }

  // Agents
  async listAgents() {
    return this.request<VoiceAgent[]>('/api/v1/agents');
  }

  async createAgent(data: Partial<VoiceAgent> & { name: string; system_prompt: string }) {
    return this.request<VoiceAgent>('/api/v1/agents', { method: 'POST', body: JSON.stringify(data) });
  }

  async generateAgentDraft(data: AgentAIDraftRequest) {
    return this.request<AgentAIDraftResponse>('/api/v1/agents/ai-draft', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async getAgent(id: string) {
    return this.request<VoiceAgent>(`/api/v1/agents/${id}`);
  }

  async updateAgent(
    id: string,
    data: Partial<VoiceAgent>,
    options: { deprovisionExistingProvider?: boolean } = {},
  ) {
    const query = options.deprovisionExistingProvider
      ? '?deprovision_existing_provider=true'
      : '';
    return this.request<VoiceAgent>(`/api/v1/agents/${id}${query}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  }

  async deleteAgent(id: string) {
    return this.request<void>(`/api/v1/agents/${id}`, { method: 'DELETE' });
  }

  async getProviderStatus() {
    return this.request<ProviderStatus>('/api/v1/agents/provider/status');
  }

  async saveSarvamCredential(apiKey: string) {
    return this.request<ProviderCredentialStatus>('/api/v1/agents/provider/sarvam/credential', {
      method: 'PUT',
      body: JSON.stringify({ api_key: apiKey }),
    });
  }

  async deleteSarvamCredential() {
    return this.request<ProviderCredentialStatus>('/api/v1/agents/provider/sarvam/credential', {
      method: 'DELETE',
    });
  }

  async listProviderCredentials() {
    return this.request<WorkspaceCredentialStatuses>('/api/v1/runtime/credentials');
  }

  async saveProviderApiKey(provider: Exclude<WorkspaceProviderName, 'twilio'>, apiKey: string) {
    return this.request<WorkspaceCredentialStatus>(`/api/v1/runtime/credentials/${provider}`, {
      method: 'PUT',
      body: JSON.stringify({ api_key: apiKey }),
    });
  }

  async saveTwilioCredential(data: {
    account_sid: string;
    auth_token: string;
    default_from_number: string | null;
  }) {
    return this.request<WorkspaceCredentialStatus>('/api/v1/runtime/credentials/twilio/account', {
      method: 'PUT',
      body: JSON.stringify(data),
    });
  }

  async deleteProviderCredential(provider: WorkspaceProviderName) {
    return this.request<WorkspaceCredentialStatus>(`/api/v1/runtime/credentials/${provider}`, {
      method: 'DELETE',
    });
  }

  async listRuntimeProfiles() {
    return this.request<RuntimeProfile[]>('/api/v1/runtime/agents');
  }

  async getRuntimeProfile(agentId: string) {
    return this.request<RuntimeProfile>(`/api/v1/runtime/agents/${agentId}`);
  }

  async updateRuntimeProfile(agentId: string, data: Omit<RuntimeProfile,
    'id' | 'agent_id' | 'enabled' | 'status' | 'ready' | 'blockers' |
    'last_tested_at' | 'created_at' | 'updated_at'>) {
    return this.request<RuntimeProfile>(`/api/v1/runtime/agents/${agentId}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    });
  }

  async testRuntimeProfile(agentId: string) {
    return this.request<RuntimeReadiness>(`/api/v1/runtime/agents/${agentId}/test`, {
      method: 'POST',
    });
  }

  async createLiveKitBrowserSession(
    agentId: string,
    variables: Record<string, string | number | boolean> = {},
  ) {
    const idempotencyKey = globalThis.crypto?.randomUUID?.();
    if (!idempotencyKey) throw new Error('This browser cannot create a secure call identity.');
    return this.request<LiveKitBrowserSession>(
      `/api/v1/agents/${agentId}/livekit/session`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': idempotencyKey },
        body: JSON.stringify({ variables }),
      },
    );
  }

  async activateRuntimeProfile(agentId: string) {
    return this.request<RuntimeProfile>(`/api/v1/runtime/agents/${agentId}/activate`, {
      method: 'POST',
    });
  }

  async deactivateRuntimeProfile(agentId: string) {
    return this.request<RuntimeProfile>(`/api/v1/runtime/agents/${agentId}/deactivate`, {
      method: 'POST',
    });
  }

  async getSipCredentialStatus() {
    return this.request<SipCredentialStatus>('/api/v1/runtime/sip/credential');
  }

  async saveSipCredential(data: {
    sip_uri: string;
    inbound_trunk_id: string;
    dispatch_rule_id: string;
    outbound_trunk_id: string | null;
    agent_name: string;
  }) {
    return this.request<SipCredentialStatus>('/api/v1/runtime/sip/credential', {
      method: 'PUT',
      body: JSON.stringify(data),
    });
  }

  async deleteSipCredential() {
    return this.request<SipCredentialStatus>('/api/v1/runtime/sip/credential', {
      method: 'DELETE',
    });
  }

  async getAgentProviderCatalog() {
    return this.request<AgentProviderCatalog>('/api/v1/agents/provider/catalog');
  }

  async previewVoice(provider: 'smallest' | 'sarvam' | 'elevenlabs' | 'inworld', voiceId: string, language?: string) {
    return this.requestBlob('/api/v1/agents/provider/voice-preview', {
      method: 'POST',
      body: JSON.stringify({ provider, voice_id: voiceId, language }),
    });
  }

  async listVoiceClones() {
    return this.request<VoiceClone[]>('/api/v1/agents/provider/voice-clones');
  }

  async createVoiceClone(data: {
    displayName: string;
    language: string;
    accent: string;
    gender: 'female' | 'male' | '';
    description: string;
    model: 'lightning-v3.1' | 'lightning-v3.1-pro';
    consentConfirmed: boolean;
    file: File;
  }) {
    const form = new FormData();
    form.append('display_name', data.displayName);
    form.append('language', data.language);
    form.append('accent', data.accent);
    form.append('gender', data.gender);
    form.append('description', data.description);
    form.append('model', data.model);
    form.append('consent_confirmed', String(data.consentConfirmed));
    form.append('media', data.file);
    return this.request<VoiceClone>('/api/v1/agents/provider/voice-clones', {
      method: 'POST',
      body: form,
    });
  }

  async refreshVoiceClone(id: string) {
    return this.request<VoiceClone>(`/api/v1/agents/provider/voice-clones/${id}/refresh`, {
      method: 'POST',
    });
  }

  async deleteVoiceClone(id: string) {
    return this.request<void>(`/api/v1/agents/provider/voice-clones/${id}`, {
      method: 'DELETE',
    });
  }

  async provisionSmallestAgent(id: string) {
    return this.request<VoiceAgent>(`/api/v1/agents/${id}/smallest/provision`, { method: 'POST' });
  }

  async syncSmallestAgent(id: string) {
    return this.request<VoiceAgent>(`/api/v1/agents/${id}/smallest/sync`, { method: 'POST' });
  }

  async resolveSmallestAgent(id: string, data: {
    action: 'confirm_create_absent' | 'confirm_publish_absent';
    confirmation: string;
  }) {
    return this.request<VoiceAgent>(`/api/v1/agents/${id}/smallest/resolve`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async createSmallestSession(
    id: string,
    variables: Record<string, string | number | boolean> = {},
  ) {
    return this.request<BrowserSession>(`/api/v1/agents/${id}/smallest/session`, {
      method: 'POST',
      body: JSON.stringify({ variables }),
    });
  }

  // Knowledge Studio
  async listKnowledgeBases() {
    return this.request<KnowledgeBase[]>('/api/v1/knowledge');
  }

  async createKnowledgeBase(data: {
    name: string;
    description?: string;
    scope_type?: KnowledgeScope;
    scope_label?: string;
    languages?: string[];
    tags?: string[];
  }) {
    return this.request<KnowledgeBase>('/api/v1/knowledge', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async getKnowledgeBase(id: string) {
    return this.request<KnowledgeBase>(`/api/v1/knowledge/${id}`);
  }

  async updateKnowledgeBase(id: string, data: Partial<Pick<KnowledgeBase,
    'name' | 'description' | 'scope_type' | 'scope_label' | 'languages' | 'tags'>>) {
    return this.request<KnowledgeBase>(`/api/v1/knowledge/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  }

  async provisionKnowledgeBase(id: string) {
    return this.request<KnowledgeBase>(`/api/v1/knowledge/${id}/provision`, { method: 'POST' });
  }

  async discoverKnowledgeSitemap(id: string, sitemapUrl: string) {
    return this.request<{ urls: string[] }>(`/api/v1/knowledge/${id}/sitemap/discover`, {
      method: 'POST',
      body: JSON.stringify({ sitemap_url: sitemapUrl }),
    });
  }

  async addKnowledgeUrls(id: string, urls: string[]) {
    return this.request<KnowledgeBase>(`/api/v1/knowledge/${id}/sources/urls`, {
      method: 'POST',
      body: JSON.stringify({ urls }),
    });
  }

  async addKnowledgeText(id: string, name: string, content: string) {
    return this.request<KnowledgeBase>(`/api/v1/knowledge/${id}/sources/text`, {
      method: 'POST',
      body: JSON.stringify({ name, content }),
    });
  }

  async uploadKnowledgePdf(id: string, file: File) {
    const form = new FormData();
    form.append('media', file);
    return this.request<KnowledgeBase>(`/api/v1/knowledge/${id}/sources/pdf`, {
      method: 'POST',
      body: form,
    });
  }

  async deleteKnowledgeSource(id: string, sourceId: string) {
    return this.request<KnowledgeBase>(`/api/v1/knowledge/${id}/sources/${sourceId}`, {
      method: 'DELETE',
    });
  }

  async startKnowledgeCrawl(id: string, data: {
    homepage_url: string;
    max_pages: number;
    max_depth: number;
    include_subdomains: boolean;
    processing_mode: KnowledgeProcessingMode;
  }) {
    return this.request<KnowledgeBase>(`/api/v1/knowledge/${id}/crawls`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async generateKnowledgeDraft(data: KnowledgeAIDraftRequest) {
    return this.request<KnowledgeAIDraftResponse>('/api/v1/knowledge/ai-draft', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async retryKnowledgeCrawl(id: string, crawlId: string) {
    return this.request<KnowledgeBase>(`/api/v1/knowledge/${id}/crawls/${crawlId}/retry`, {
      method: 'POST',
    });
  }

  async repairKnowledgeSource(id: string, sourceId: string) {
    return this.request<KnowledgeBase>(`/api/v1/knowledge/${id}/sources/${sourceId}/repair`, {
      method: 'POST',
    });
  }

  async refreshKnowledgeBase(id: string) {
    return this.request<KnowledgeBase>(`/api/v1/knowledge/${id}/refresh`, { method: 'POST' });
  }

  async approveKnowledgeBase(id: string, approved: boolean) {
    return this.request<KnowledgeBase>(`/api/v1/knowledge/${id}/approval`, {
      method: 'POST',
      body: JSON.stringify({ approved }),
    });
  }

  async listKnowledgeReleases(id: string) {
    return this.request<KnowledgeServingRevision[]>(`/api/v1/knowledge/${id}/releases`);
  }

  async reactivateKnowledgeRelease(id: string, revisionId: string, data: {
    expected_current_revision_id: string | null;
    reason: string;
  }) {
    return this.request<KnowledgeBase>(`/api/v1/knowledge/${id}/releases/${revisionId}/activate`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async bindKnowledgeAgent(id: string, agentId: string) {
    return this.request<KnowledgeBase>(`/api/v1/knowledge/${id}/bindings`, {
      method: 'POST',
      body: JSON.stringify({ agent_id: agentId }),
    });
  }

  async unbindKnowledgeAgent(id: string, agentId: string) {
    return this.request<KnowledgeBase>(`/api/v1/knowledge/${id}/bindings/${agentId}`, {
      method: 'DELETE',
    });
  }

  async deleteKnowledgeBase(id: string) {
    return this.request<void>(`/api/v1/knowledge/${id}`, { method: 'DELETE' });
  }

  // Calls
  async listCalls(params?: Record<string, string>) {
    const query = params ? '?' + new URLSearchParams(params).toString() : '';
    return this.request<CallRecord[]>(`/api/v1/calls${query}`);
  }

  async registerBrowserConversation(agentId: string, providerCallId: string) {
    return this.request<CallRecord>('/api/v1/calls/browser-sessions', {
      method: 'POST',
      body: JSON.stringify({ agent_id: agentId, provider_call_id: providerCallId }),
    });
  }

  async syncProviderConversationHistory() {
    return this.request<ProviderHistorySyncResult>('/api/v1/calls/sync-provider-history', {
      method: 'POST',
    });
  }

  async getCall(id: string) {
    return this.request<CallRecord>(`/api/v1/calls/${id}`);
  }

  async initiateCall(data: {
    agent_id: string;
    to_number: string;
    context?: Record<string, string | number | boolean>;
  }, idempotencyKey?: string) {
    // Generate exactly once per deliberate invocation. request() reuses this
    // options object during an authentication retry, preserving deduplication.
    const requestKey = idempotencyKey?.trim() || createIdempotencyKey();
    return this.request<CallRecord>('/api/v1/calls', {
      method: 'POST',
      headers: { 'Idempotency-Key': requestKey },
      body: JSON.stringify(data),
    });
  }

  async getCallTranscript(callId: string) {
    return this.request<CallTranscript>(`/api/v1/calls/${callId}/transcript`);
  }

  async getCallRecording(callId: string) {
    return this.requestBlob(`/api/v1/calls/${callId}/recording`);
  }

  async getCallSummary(callId: string) {
    return this.request<CallSummary>(`/api/v1/calls/${callId}/summary`);
  }

  async reanalyzeCall(callId: string) {
    return this.request<{ status: string; call_id: string }>(`/api/v1/calls/${callId}/reanalyze`, {
      method: 'POST',
    });
  }

  // Campaigns
  async listCampaigns() {
    return this.request<Campaign[]>('/api/v1/campaigns');
  }

  async createCampaign(data: CampaignCreateRequest) {
    return this.request<Campaign>('/api/v1/campaigns', { method: 'POST', body: JSON.stringify(data) });
  }

  async startCampaign(id: string) {
    return this.request<Campaign>(`/api/v1/campaigns/${id}/start`, { method: 'POST' });
  }

  async pauseCampaign(id: string) {
    return this.request<Campaign>(`/api/v1/campaigns/${id}/pause`, { method: 'POST' });
  }

  async listCampaignAttempts(id: string, state = 'unknown') {
    const query = new URLSearchParams({ state }).toString();
    return this.request<CampaignAttempt[]>(`/api/v1/campaigns/${id}/attempts?${query}`);
  }

  async reconcileCampaignAttempt(
    campaignId: string,
    attemptId: string,
    data: CampaignAttemptReconciliation,
  ) {
    return this.request<CampaignAttempt>(
      `/api/v1/campaigns/${campaignId}/attempts/${attemptId}/reconcile`,
      { method: 'POST', body: JSON.stringify(data) },
    );
  }

  // Analytics
  async getOverview(days = 30, agentId?: string) {
    const query = new URLSearchParams({ days: String(days) });
    if (agentId) query.set('agent_id', agentId);
    return this.request<AnalyticsOverview>(`/api/v1/analytics/overview?${query}`);
  }

  async getTimeseries(days = 30, period: 'day' | 'week' | 'month' = 'day', agentId?: string) {
    const query = new URLSearchParams({ days: String(days), period });
    if (agentId) query.set('agent_id', agentId);
    return this.request<AnalyticsTimeSeries>(
      `/api/v1/analytics/timeseries?${query}`,
    );
  }

  async getAgentPerformance(days = 30) {
    return this.request<AgentPerformance[]>(`/api/v1/analytics/agents?days=${days}`);
  }

  // Workflows
  async listWorkflows() {
    return this.request<CallWorkflow[]>('/api/v1/workflows');
  }

  async createWorkflow(data: WorkflowCreateRequest) {
    return this.request<CallWorkflow>('/api/v1/workflows', { method: 'POST', body: JSON.stringify(data) });
  }

  async updateWorkflow(id: string, data: WorkflowUpdateRequest) {
    return this.request<CallWorkflow>(`/api/v1/workflows/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  }

  async deleteWorkflow(id: string) {
    return this.request<void>(`/api/v1/workflows/${id}`, { method: 'DELETE' });
  }

  // Integrations
  async listIntegrations() {
    return this.request<Integration[]>('/api/v1/integrations');
  }

  async createIntegration(data: IntegrationCreateRequest) {
    return this.request<Integration>('/api/v1/integrations', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async updateIntegration(id: string, data: IntegrationUpdateRequest) {
    return this.request<Integration>(`/api/v1/integrations/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  }

  async deleteIntegration(id: string) {
    return this.request<void>(`/api/v1/integrations/${id}`, { method: 'DELETE' });
  }

  async listIntegrationDeliveries(id: string) {
    return this.request<IntegrationDelivery[]>(`/api/v1/integrations/${id}/deliveries`);
  }

  async testIntegration(id: string) {
    return this.request<IntegrationDelivery>(`/api/v1/integrations/${id}/test`, {
      method: 'POST',
    });
  }

  async replayIntegrationDelivery(id: string, deliveryId: string) {
    return this.request<IntegrationDelivery>(
      `/api/v1/integrations/${id}/deliveries/${deliveryId}/replay`,
      { method: 'POST' },
    );
  }

  // Compliance
  async checkDnc(phone: string) {
    return this.request<{ is_on_dnc: boolean }>(`/api/v1/compliance/dnc/check?phone_number=${encodeURIComponent(phone)}`);
  }

  async addDnc(data: { phone_number: string; reason?: string }) {
    return this.request<DncEntry>('/api/v1/compliance/dnc', { method: 'POST', body: JSON.stringify(data) });
  }

  async listConsentRecords(phone?: string) {
    const query = phone ? `?phone_number=${encodeURIComponent(phone)}` : '';
    return this.request<ConsentRecord[]>(`/api/v1/compliance/consent${query}`);
  }

  async createConsentRecord(data: {
    phone_number: string;
    consent_type: ConsentType;
    status: 'granted' | 'revoked';
    evidence?: Record<string, string>;
  }) {
    return this.request<ConsentRecord>('/api/v1/compliance/consent', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Billing
  async getUsage(days = 30) {
    return this.request<UsageSummary>(`/api/v1/billing/usage?days=${days}`);
  }

  async getPlans() {
    return this.request<BillingPlan[]>('/api/v1/billing/plans');
  }

  private costReportQuery(filters: CostReportFilters = {}) {
    const query = new URLSearchParams();
    query.set('days', String(filters.days || 30));
    if (filters.provider) query.set('provider', filters.provider);
    if (filters.speech_provider) query.set('speech_provider', filters.speech_provider);
    if (filters.agent_id) query.set('agent_id', filters.agent_id);
    if (filters.direction) query.set('direction', filters.direction);
    if (filters.status) query.set('status', filters.status);
    return query.toString();
  }

  async getCostReport(filters: CostReportFilters = {}) {
    return this.request<CostReport>(`/api/v1/billing/cost-report?${this.costReportQuery(filters)}`);
  }

  async downloadCostReport(filters: CostReportFilters = {}) {
    return this.requestBlob(
      `/api/v1/billing/cost-report.csv?${this.costReportQuery(filters)}`,
    );
  }
}

export const api = new ApiClient();
