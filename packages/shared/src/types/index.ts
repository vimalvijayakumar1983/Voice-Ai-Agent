// ============================================================
// Voice AI Agent Platform - Shared Types & Enums
// ============================================================

// ---------------------
// User & Auth
// ---------------------

export enum UserRole {
  SUPER_ADMIN = 'SUPER_ADMIN',
  TENANT_OWNER = 'TENANT_OWNER',
  ADMIN = 'ADMIN',
  MANAGER = 'MANAGER',
  SUPERVISOR = 'SUPERVISOR',
  QA_AUDITOR = 'QA_AUDITOR',
  AGENT_OPERATOR = 'AGENT_OPERATOR',
  READONLY_ANALYST = 'READONLY_ANALYST',
}

export enum UserStatus {
  ACTIVE = 'ACTIVE',
  INACTIVE = 'INACTIVE',
  SUSPENDED = 'SUSPENDED',
  PENDING_VERIFICATION = 'PENDING_VERIFICATION',
}

// ---------------------
// Industry & Agent
// ---------------------

export enum Industry {
  HEALTHCARE = 'HEALTHCARE',
  FINANCIAL = 'FINANCIAL',
  LEGAL = 'LEGAL',
  COLLECTIONS = 'COLLECTIONS',
  SALES = 'SALES',
  CUSTOMER_SUPPORT = 'CUSTOMER_SUPPORT',
  GENERAL = 'GENERAL',
}

export enum AgentType {
  INBOUND_SUPPORT = 'INBOUND_SUPPORT',
  OUTBOUND_CAMPAIGN = 'OUTBOUND_CAMPAIGN',
  APPOINTMENT_ASSISTANT = 'APPOINTMENT_ASSISTANT',
  COLLECTIONS_AGENT = 'COLLECTIONS_AGENT',
  SALES_QUALIFIER = 'SALES_QUALIFIER',
  LEGAL_INTAKE = 'LEGAL_INTAKE',
  SURVEY_FEEDBACK = 'SURVEY_FEEDBACK',
}

// ---------------------
// Call
// ---------------------

export enum CallStatus {
  QUEUED = 'QUEUED',
  RINGING = 'RINGING',
  IN_PROGRESS = 'IN_PROGRESS',
  COMPLETED = 'COMPLETED',
  FAILED = 'FAILED',
  VOICEMAIL = 'VOICEMAIL',
  NO_ANSWER = 'NO_ANSWER',
  BUSY = 'BUSY',
  TRANSFERRED = 'TRANSFERRED',
  CANCELLED = 'CANCELLED',
}

export enum CallDirection {
  INBOUND = 'INBOUND',
  OUTBOUND = 'OUTBOUND',
}

export enum CallDisposition {
  SUCCESSFUL = 'SUCCESSFUL',
  CALLBACK_REQUESTED = 'CALLBACK_REQUESTED',
  NOT_INTERESTED = 'NOT_INTERESTED',
  WRONG_NUMBER = 'WRONG_NUMBER',
  DO_NOT_CALL = 'DO_NOT_CALL',
  ESCALATED = 'ESCALATED',
  LEFT_VOICEMAIL = 'LEFT_VOICEMAIL',
  TECHNICAL_FAILURE = 'TECHNICAL_FAILURE',
  NO_DISPOSITION = 'NO_DISPOSITION',
}

// ---------------------
// Campaign
// ---------------------

export enum CampaignStatus {
  DRAFT = 'DRAFT',
  SCHEDULED = 'SCHEDULED',
  RUNNING = 'RUNNING',
  PAUSED = 'PAUSED',
  COMPLETED = 'COMPLETED',
  CANCELLED = 'CANCELLED',
}

export enum CampaignType {
  OUTBOUND_BLAST = 'OUTBOUND_BLAST',
  DRIP = 'DRIP',
  PREDICTIVE = 'PREDICTIVE',
  PROGRESSIVE = 'PROGRESSIVE',
}

// ---------------------
// Workflow
// ---------------------

export enum WorkflowNodeType {
  GREETING = 'GREETING',
  CONSENT_CAPTURE = 'CONSENT_CAPTURE',
  VERIFICATION = 'VERIFICATION',
  QUALIFICATION = 'QUALIFICATION',
  INFO_CAPTURE = 'INFO_CAPTURE',
  LOOKUP = 'LOOKUP',
  SUMMARIZE = 'SUMMARIZE',
  OFFER_OPTIONS = 'OFFER_OPTIONS',
  BOOK_APPOINTMENT = 'BOOK_APPOINTMENT',
  COLLECT_PAYMENT = 'COLLECT_PAYMENT',
  ESCALATE_HUMAN = 'ESCALATE_HUMAN',
  SEND_MESSAGE = 'SEND_MESSAGE',
  END_CALL = 'END_CALL',
  RETRY_LATER = 'RETRY_LATER',
  MARK_UNREACHABLE = 'MARK_UNREACHABLE',
  DNC_OPTOUT = 'DNC_OPTOUT',
  DISPUTE = 'DISPUTE',
  COMPLAINT = 'COMPLAINT',
}

export enum WorkflowStatus {
  DRAFT = 'DRAFT',
  PUBLISHED = 'PUBLISHED',
  ARCHIVED = 'ARCHIVED',
}

// ---------------------
// Provider
// ---------------------

export enum TelephonyProvider {
  TWILIO = 'TWILIO',
  EXOTEL = 'EXOTEL',
  PLIVO = 'PLIVO',
  VONAGE = 'VONAGE',
  SIP = 'SIP',
}

export enum LLMProvider {
  ANTHROPIC = 'ANTHROPIC',
  OPENAI = 'OPENAI',
  GOOGLE = 'GOOGLE',
}

export enum STTProvider {
  DEEPGRAM = 'DEEPGRAM',
  GOOGLE = 'GOOGLE',
  AZURE = 'AZURE',
  WHISPER = 'WHISPER',
}

export enum TTSProvider {
  ELEVENLABS = 'ELEVENLABS',
  GOOGLE = 'GOOGLE',
  AZURE = 'AZURE',
  AMAZON_POLLY = 'AMAZON_POLLY',
}

// ---------------------
// Contact
// ---------------------

export enum ContactStatus {
  ACTIVE = 'ACTIVE',
  INACTIVE = 'INACTIVE',
  BLACKLISTED = 'BLACKLISTED',
  UNREACHABLE = 'UNREACHABLE',
  DO_NOT_CALL = 'DO_NOT_CALL',
}

export enum ConsentStatus {
  GRANTED = 'GRANTED',
  DENIED = 'DENIED',
  PENDING = 'PENDING',
  REVOKED = 'REVOKED',
  EXPIRED = 'EXPIRED',
}

export enum ConsentType {
  CALL_RECORDING = 'CALL_RECORDING',
  DATA_PROCESSING = 'DATA_PROCESSING',
  MARKETING = 'MARKETING',
  SMS = 'SMS',
}

// ---------------------
// Compliance
// ---------------------

export enum ComplianceRegion {
  TCPA = 'TCPA',
  GDPR = 'GDPR',
  HIPAA = 'HIPAA',
  PCI_DSS = 'PCI_DSS',
  CCPA = 'CCPA',
  TRAI = 'TRAI',
}

// ---------------------
// Webhook
// ---------------------

export enum WebhookEvent {
  CALL_STARTED = 'call.started',
  CALL_COMPLETED = 'call.completed',
  CALL_FAILED = 'call.failed',
  CAMPAIGN_STARTED = 'campaign.started',
  CAMPAIGN_COMPLETED = 'campaign.completed',
  CONTACT_OPTED_OUT = 'contact.opted_out',
  RECORDING_READY = 'recording.ready',
  TRANSCRIPT_READY = 'transcript.ready',
}

// ============================================================
// Interfaces
// ============================================================

// ---------------------
// Pagination
// ---------------------

export interface PaginationParams {
  page: number;
  limit: number;
  sortBy?: string;
  sortOrder?: 'asc' | 'desc';
}

export interface PaginatedResponse<T> {
  data: T[];
  meta: {
    total: number;
    page: number;
    limit: number;
    totalPages: number;
    hasNextPage: boolean;
    hasPreviousPage: boolean;
  };
}

// ---------------------
// API Response
// ---------------------

export interface ApiResponse<T = unknown> {
  success: boolean;
  data: T;
  message?: string;
  timestamp: string;
}

export interface ApiErrorResponse {
  success: false;
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  };
  timestamp: string;
}

// ---------------------
// Auth
// ---------------------

export interface LoginRequest {
  email: string;
  password: string;
  tenantSlug?: string;
}

export interface LoginResponse {
  accessToken: string;
  refreshToken: string;
  expiresIn: number;
  user: UserProfile;
}

export interface RefreshTokenRequest {
  refreshToken: string;
}

export interface UserProfile {
  id: string;
  email: string;
  firstName: string;
  lastName: string;
  role: UserRole;
  status: UserStatus;
  tenantId: string;
  tenantName: string;
  avatarUrl?: string;
  lastLoginAt?: string;
  createdAt: string;
}

// ---------------------
// Tenant
// ---------------------

export interface Tenant {
  id: string;
  name: string;
  slug: string;
  industry: Industry;
  logoUrl?: string;
  isActive: boolean;
  settings: TenantSettings;
  createdAt: string;
  updatedAt: string;
}

export interface TenantSettings {
  maxConcurrentCalls: number;
  maxAgents: number;
  defaultTelephonyProvider: TelephonyProvider;
  defaultLLMProvider: LLMProvider;
  defaultSTTProvider: STTProvider;
  defaultTTSProvider: TTSProvider;
  complianceRegions: ComplianceRegion[];
  callRecordingEnabled: boolean;
  callRecordingConsentRequired: boolean;
  timezone: string;
  businessHours: BusinessHours;
}

export interface BusinessHours {
  enabled: boolean;
  schedule: {
    [day in DayOfWeek]?: { start: string; end: string } | null;
  };
  timezone: string;
}

export type DayOfWeek =
  | 'monday'
  | 'tuesday'
  | 'wednesday'
  | 'thursday'
  | 'friday'
  | 'saturday'
  | 'sunday';

// ---------------------
// Voice Agent
// ---------------------

export interface VoiceAgent {
  id: string;
  tenantId: string;
  name: string;
  description?: string;
  type: AgentType;
  industry: Industry;
  telephonyProvider: TelephonyProvider;
  llmProvider: LLMProvider;
  sttProvider: STTProvider;
  ttsProvider: TTSProvider;
  voiceId: string;
  systemPrompt: string;
  workflowId?: string;
  maxCallDurationSeconds: number;
  isActive: boolean;
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

export interface CreateVoiceAgentRequest {
  name: string;
  description?: string;
  type: AgentType;
  industry: Industry;
  telephonyProvider?: TelephonyProvider;
  llmProvider?: LLMProvider;
  sttProvider?: STTProvider;
  ttsProvider?: TTSProvider;
  voiceId?: string;
  systemPrompt: string;
  workflowId?: string;
  maxCallDurationSeconds?: number;
  metadata?: Record<string, unknown>;
}

export interface UpdateVoiceAgentRequest extends Partial<CreateVoiceAgentRequest> {
  isActive?: boolean;
}

// ---------------------
// Call
// ---------------------

export interface Call {
  id: string;
  tenantId: string;
  agentId: string;
  campaignId?: string;
  contactId: string;
  direction: CallDirection;
  status: CallStatus;
  disposition?: CallDisposition;
  fromNumber: string;
  toNumber: string;
  telephonyProvider: TelephonyProvider;
  telephonyCallSid?: string;
  startedAt?: string;
  answeredAt?: string;
  endedAt?: string;
  durationSeconds?: number;
  recordingUrl?: string;
  transcriptUrl?: string;
  summary?: string;
  sentiment?: CallSentiment;
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

export interface CallSentiment {
  overall: 'positive' | 'neutral' | 'negative';
  score: number;
  segments: SentimentSegment[];
}

export interface SentimentSegment {
  startTime: number;
  endTime: number;
  sentiment: 'positive' | 'neutral' | 'negative';
  score: number;
  text: string;
}

export interface InitiateCallRequest {
  agentId: string;
  contactId: string;
  phoneNumber: string;
  campaignId?: string;
  scheduledAt?: string;
  metadata?: Record<string, unknown>;
}

// ---------------------
// Campaign
// ---------------------

export interface Campaign {
  id: string;
  tenantId: string;
  name: string;
  description?: string;
  type: CampaignType;
  status: CampaignStatus;
  agentId: string;
  contactListId: string;
  workflowId?: string;
  scheduledStartAt?: string;
  scheduledEndAt?: string;
  actualStartedAt?: string;
  actualEndedAt?: string;
  maxConcurrentCalls: number;
  callsPerMinute: number;
  retryAttempts: number;
  retryDelayMinutes: number;
  stats: CampaignStats;
  createdAt: string;
  updatedAt: string;
}

export interface CampaignStats {
  totalContacts: number;
  contactsCalled: number;
  contactsReached: number;
  contactsCompleted: number;
  contactsFailed: number;
  contactsPending: number;
  averageCallDuration: number;
  successRate: number;
}

export interface CreateCampaignRequest {
  name: string;
  description?: string;
  type: CampaignType;
  agentId: string;
  contactListId: string;
  workflowId?: string;
  scheduledStartAt?: string;
  scheduledEndAt?: string;
  maxConcurrentCalls?: number;
  callsPerMinute?: number;
  retryAttempts?: number;
  retryDelayMinutes?: number;
}

export interface UpdateCampaignRequest extends Partial<CreateCampaignRequest> {
  status?: CampaignStatus;
}

// ---------------------
// Contact
// ---------------------

export interface Contact {
  id: string;
  tenantId: string;
  firstName: string;
  lastName: string;
  phoneNumber: string;
  alternatePhone?: string;
  email?: string;
  status: ContactStatus;
  consentStatus: ConsentStatus;
  timezone?: string;
  tags: string[];
  customFields: Record<string, unknown>;
  lastContactedAt?: string;
  createdAt: string;
  updatedAt: string;
}

export interface CreateContactRequest {
  firstName: string;
  lastName: string;
  phoneNumber: string;
  alternatePhone?: string;
  email?: string;
  timezone?: string;
  tags?: string[];
  customFields?: Record<string, unknown>;
}

export interface UpdateContactRequest extends Partial<CreateContactRequest> {
  status?: ContactStatus;
  consentStatus?: ConsentStatus;
}

// ---------------------
// Workflow
// ---------------------

export interface Workflow {
  id: string;
  tenantId: string;
  name: string;
  description?: string;
  status: WorkflowStatus;
  version: number;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  createdAt: string;
  updatedAt: string;
}

export interface WorkflowNode {
  id: string;
  type: WorkflowNodeType;
  label: string;
  position: { x: number; y: number };
  config: Record<string, unknown>;
}

export interface WorkflowEdge {
  id: string;
  source: string;
  target: string;
  condition?: string;
  label?: string;
}

export interface CreateWorkflowRequest {
  name: string;
  description?: string;
  nodes: Omit<WorkflowNode, 'id'>[];
  edges: Omit<WorkflowEdge, 'id'>[];
}

// ---------------------
// Webhook
// ---------------------

export interface WebhookConfig {
  id: string;
  tenantId: string;
  url: string;
  secret: string;
  events: WebhookEvent[];
  isActive: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface WebhookPayload<T = unknown> {
  id: string;
  event: WebhookEvent;
  tenantId: string;
  timestamp: string;
  data: T;
}

// ---------------------
// Analytics / Dashboard
// ---------------------

export interface DashboardStats {
  totalCalls: number;
  activeCalls: number;
  totalCampaigns: number;
  activeCampaigns: number;
  totalContacts: number;
  totalAgents: number;
  averageCallDuration: number;
  successRate: number;
  callsToday: number;
  callsTrend: TrendData[];
}

export interface TrendData {
  date: string;
  value: number;
}

export interface CallAnalytics {
  period: string;
  totalCalls: number;
  completedCalls: number;
  failedCalls: number;
  averageDuration: number;
  averageSentiment: number;
  callsByStatus: Record<CallStatus, number>;
  callsByDisposition: Record<CallDisposition, number>;
  callsByHour: { hour: number; count: number }[];
  topAgents: { agentId: string; agentName: string; calls: number; successRate: number }[];
}

// ============================================================
// Phase 2: Campaign Execution & Call Orchestration
// ============================================================

export enum CampaignContactStatus {
  PENDING = 'PENDING',
  QUEUED = 'QUEUED',
  CALLING = 'CALLING',
  COMPLETED = 'COMPLETED',
  FAILED = 'FAILED',
  SKIPPED = 'SKIPPED',
  RETRY = 'RETRY',
}

export enum PacingMode {
  PREDICTIVE = 'PREDICTIVE',
  PROGRESSIVE = 'PROGRESSIVE',
  PREVIEW = 'PREVIEW',
  POWER = 'POWER',
}

export interface CampaignExecutionConfig {
  pacingMode: PacingMode;
  maxConcurrentCalls: number;
  callsPerMinute: number;
  maxRetries: number;
  retryDelayMinutes: number;
  callingWindowStart: string;
  callingWindowEnd: string;
  respectTimezones: boolean;
  abandonRateTarget: number;
}

export interface LiveCallData {
  callId: string;
  contactName: string;
  phoneNumber: string;
  agentName: string;
  duration: number;
  status: CallStatus;
  sentiment: 'positive' | 'neutral' | 'negative';
  currentTranscript: TranscriptSegment[];
}

export interface TranscriptSegment {
  role: 'agent' | 'human';
  text: string;
  timestamp: number;
  sentiment?: 'positive' | 'neutral' | 'negative';
  confidence?: number;
}

export interface CallOrchestratorEvent {
  type: 'call:started' | 'call:ringing' | 'call:answered' | 'call:transcript' | 'call:ended' | 'call:error' | 'call:handoff';
  callId: string;
  tenantId: string;
  data: Record<string, unknown>;
  timestamp: string;
}

export interface AudioPipelineMetrics {
  sttLatencyMs: number;
  llmLatencyMs: number;
  ttsLatencyMs: number;
  totalLatencyMs: number;
  audioBufferMs: number;
}

// ============================================================
// Phase 3: Knowledge Base RAG
// ============================================================

export enum ChunkStrategy {
  FIXED_SIZE = 'FIXED_SIZE',
  SENTENCE = 'SENTENCE',
  SEMANTIC = 'SEMANTIC',
  PARAGRAPH = 'PARAGRAPH',
}

export interface DocumentChunkData {
  id: string;
  documentId: string;
  content: string;
  chunkIndex: number;
  tokenCount: number;
  metadata: Record<string, unknown>;
}

export interface RAGQueryResult {
  query: string;
  results: RAGRetrievedChunk[];
  answer: string;
  sources: RAGSource[];
  confidence: number;
  tokensUsed: number;
}

export interface RAGRetrievedChunk {
  chunkId: string;
  documentId: string;
  documentName: string;
  content: string;
  score: number;
  metadata: Record<string, unknown>;
}

export interface RAGSource {
  documentId: string;
  documentName: string;
  chunkIds: string[];
  relevanceScore: number;
}

export interface KnowledgeBaseStats {
  id: string;
  name: string;
  documentCount: number;
  chunkCount: number;
  totalTokens: number;
  lastUpdated: string;
}

// ============================================================
// Phase 3: CRM Integration
// ============================================================

export enum CrmProvider {
  SALESFORCE = 'SALESFORCE',
  HUBSPOT = 'HUBSPOT',
}

export enum SyncDirection {
  INBOUND = 'INBOUND',
  OUTBOUND = 'OUTBOUND',
  BIDIRECTIONAL = 'BIDIRECTIONAL',
}

export interface CrmFieldMapping {
  sourceField: string;
  targetField: string;
  direction: SyncDirection;
  transform?: string;
  isRequired: boolean;
  defaultValue?: string;
}

export interface CrmSyncStatus {
  integrationId: string;
  provider: CrmProvider;
  status: 'idle' | 'syncing' | 'success' | 'failed';
  lastSyncAt?: string;
  syncedRecords: number;
  failedRecords: number;
  errors: string[];
}

export interface CrmContact {
  externalId: string;
  firstName: string;
  lastName: string;
  email?: string;
  phone?: string;
  company?: string;
  title?: string;
  customFields: Record<string, unknown>;
}

// ============================================================
// Phase 3: Billing
// ============================================================

export enum BillingPlan {
  FREE = 'FREE',
  STARTER = 'STARTER',
  PROFESSIONAL = 'PROFESSIONAL',
  ENTERPRISE = 'ENTERPRISE',
}

export enum UsageDimension {
  CALL_MINUTES = 'CALL_MINUTES',
  API_CALLS = 'API_CALLS',
  TRANSCRIPTION_MINUTES = 'TRANSCRIPTION_MINUTES',
  TTS_CHARACTERS = 'TTS_CHARACTERS',
  LLM_TOKENS = 'LLM_TOKENS',
  STORAGE_GB = 'STORAGE_GB',
}

export interface PlanDetails {
  id: string;
  name: BillingPlan;
  displayName: string;
  monthlyPrice: number;
  annualPrice: number;
  features: PlanFeatures;
  limits: PlanLimits;
}

export interface PlanFeatures {
  maxAgents: number;
  maxCallsPerMonth: number;
  maxConcurrentCalls: number;
  maxContacts: number;
  maxUsers: number;
  maxKnowledgeBases: number;
  maxStorageGb: number;
  includesCallMinutes: number;
  includesTranscription: boolean;
  includesCrmIntegration: boolean;
  includesAbTesting: boolean;
  includesAdvancedAnalytics: boolean;
  includesCustomWorkflows: boolean;
}

export interface PlanLimits {
  callMinutes: number;
  apiCalls: number;
  transcriptionMinutes: number;
  ttsCharacters: number;
  llmTokens: number;
  storageGb: number;
}

export interface UsageSummary {
  dimension: UsageDimension;
  used: number;
  limit: number;
  percentage: number;
  unit: string;
  cost: number;
}

export interface BillingOverview {
  plan: PlanDetails;
  subscription: {
    status: string;
    currentPeriodStart: string;
    currentPeriodEnd: string;
    cancelAtPeriodEnd: boolean;
  };
  usage: UsageSummary[];
  estimatedCost: number;
}

// ============================================================
// Phase 4: A/B Testing
// ============================================================

export enum ExperimentStatus {
  DRAFT = 'DRAFT',
  RUNNING = 'RUNNING',
  PAUSED = 'PAUSED',
  COMPLETED = 'COMPLETED',
  CANCELLED = 'CANCELLED',
}

export interface ABExperiment {
  id: string;
  tenantId: string;
  name: string;
  description?: string;
  agentId: string;
  status: ExperimentStatus;
  trafficSplit: 'equal' | 'percentage' | 'stratified';
  minSampleSize: number;
  confidenceLevel: number;
  primaryMetric: 'success_rate' | 'avg_duration' | 'sentiment' | 'conversion';
  variants: ABVariant[];
  startedAt?: string;
  completedAt?: string;
  winnerId?: string;
  createdAt: string;
  updatedAt: string;
}

export interface ABVariant {
  id: string;
  experimentId: string;
  name: string;
  description?: string;
  systemPrompt: string;
  trafficPercent: number;
  isControl: boolean;
  metrics: ABVariantMetrics;
}

export interface ABVariantMetrics {
  totalCalls: number;
  successfulCalls: number;
  failedCalls: number;
  totalDuration: number;
  avgDuration: number;
  avgSentiment: number;
  conversions: number;
  successRate: number;
  conversionRate: number;
}

export interface ABTestResults {
  experimentId: string;
  status: ExperimentStatus;
  totalSamples: number;
  isStatisticallySignificant: boolean;
  pValue: number;
  confidenceLevel: number;
  variants: ABVariantResults[];
  recommendation: string;
  winnerId?: string;
}

export interface ABVariantResults {
  variantId: string;
  name: string;
  isControl: boolean;
  metrics: ABVariantMetrics;
  confidenceInterval: { lower: number; upper: number };
  relativeImprovement?: number;
}

// ============================================================
// Phase 4: Advanced Analytics
// ============================================================

export interface AnalyticsOverview {
  kpis: {
    totalCalls: number;
    activeCalls: number;
    successRate: number;
    avgDuration: number;
    avgSentiment: number;
    totalCost: number;
    callsToday: number;
    callsTrend: number;
  };
  callVolumeTrend: TimeSeriesData[];
  outcomeDistribution: { name: string; value: number }[];
  topPerformingAgents: AgentPerformance[];
}

export interface TimeSeriesData {
  timestamp: string;
  value: number;
  label?: string;
}

export interface AgentPerformance {
  agentId: string;
  agentName: string;
  totalCalls: number;
  successRate: number;
  avgDuration: number;
  avgSentiment: number;
  score: number;
}

export interface CampaignFunnel {
  campaignId: string;
  campaignName: string;
  stages: FunnelStage[];
}

export interface FunnelStage {
  name: string;
  count: number;
  percentage: number;
  conversionFromPrevious: number;
}

export interface SentimentHeatmapData {
  dayOfWeek: number;
  hour: number;
  avgSentiment: number;
  callCount: number;
}

export interface CostBreakdown {
  period: string;
  telephony: number;
  stt: number;
  tts: number;
  llm: number;
  storage: number;
  total: number;
}

export interface ReportConfig {
  type: 'CALL_SUMMARY' | 'CAMPAIGN_PERFORMANCE' | 'AGENT_SCORECARD' | 'COMPLIANCE_AUDIT' | 'USAGE_REPORT' | 'COST_ANALYSIS';
  dateRange: { start: string; end: string };
  format: 'pdf' | 'csv' | 'xlsx';
  filters?: Record<string, unknown>;
}

// ============================================================
// Phase 4: Workflow Builder
// ============================================================

export enum ExtendedWorkflowNodeType {
  GREETING = 'GREETING',
  CONSENT_CAPTURE = 'CONSENT_CAPTURE',
  VERIFICATION = 'VERIFICATION',
  QUALIFICATION = 'QUALIFICATION',
  INFO_CAPTURE = 'INFO_CAPTURE',
  LOOKUP = 'LOOKUP',
  SUMMARIZE = 'SUMMARIZE',
  OFFER_OPTIONS = 'OFFER_OPTIONS',
  BOOK_APPOINTMENT = 'BOOK_APPOINTMENT',
  COLLECT_PAYMENT = 'COLLECT_PAYMENT',
  ESCALATE_HUMAN = 'ESCALATE_HUMAN',
  SEND_MESSAGE = 'SEND_MESSAGE',
  END_CALL = 'END_CALL',
  RETRY_LATER = 'RETRY_LATER',
  MARK_UNREACHABLE = 'MARK_UNREACHABLE',
  DNC_OPTOUT = 'DNC_OPTOUT',
  DISPUTE = 'DISPUTE',
  COMPLAINT = 'COMPLAINT',
  CONDITIONAL = 'CONDITIONAL',
  WAIT = 'WAIT',
  API_CALL = 'API_CALL',
  SET_VARIABLE = 'SET_VARIABLE',
  KNOWLEDGE_QUERY = 'KNOWLEDGE_QUERY',
  SENTIMENT_CHECK = 'SENTIMENT_CHECK',
  LOOP = 'LOOP',
  DELAY = 'DELAY',
}

export interface WorkflowNodeConfig {
  nodeType: string;
  prompt?: string;
  variables?: string[];
  conditions?: WorkflowCondition[];
  apiConfig?: {
    url: string;
    method: string;
    headers: Record<string, string>;
    body?: string;
    responseMapping?: Record<string, string>;
  };
  messageConfig?: {
    channel: 'sms' | 'email';
    template: string;
  };
  transferConfig?: {
    targetNumber?: string;
    targetQueue?: string;
    warmTransfer: boolean;
  };
  knowledgeBaseId?: string;
  maxWaitSeconds?: number;
  retryDelayMinutes?: number;
}

export interface WorkflowCondition {
  id: string;
  variable: string;
  operator: 'equals' | 'not_equals' | 'contains' | 'greater_than' | 'less_than' | 'is_empty' | 'is_not_empty' | 'matches_regex';
  value: string;
  targetNodeId: string;
}

export interface WorkflowSimulationStep {
  nodeId: string;
  nodeType: string;
  input: string;
  output: string;
  variables: Record<string, unknown>;
  duration: number;
  nextNodeId?: string;
}

export interface WorkflowSimulationResult {
  workflowId: string;
  steps: WorkflowSimulationStep[];
  totalDuration: number;
  outcome: string;
  variables: Record<string, unknown>;
}
