const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export interface VoiceAgent {
  id: string;
  name: string;
  description: string | null;
  is_active: boolean;
  system_prompt: string;
  model_provider: string;
  model_name: string;
  voice_provider: string;
  voice_id: string;
  language: string;
  supported_languages: string[];
  speech_rate: number;
  temperature: number;
  greeting_message: string | null;
  timezone: string;
  provider_agent_id: string | null;
  provider_branch_id: string | null;
  provider_revision_id: string | null;
  sync_status: 'local_only' | 'dirty' | 'publishing' | 'synced' | 'error';
  last_synced_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProviderStatus {
  provider: 'smallest';
  configured: boolean;
  webhook_configured: boolean;
  base_url: string;
}

export interface VoiceCatalogItem {
  id: string;
  name: string;
  languages: string[];
  accent: string | null;
  gender: string | null;
  age: string | null;
  use_cases: string[];
  source: 'catalog' | 'cloned';
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
}

export interface AgentProviderCatalog {
  provider: 'smallest';
  voice_model: string;
  voices: VoiceCatalogItem[];
  languages: LanguageCatalogItem[];
  templates: AgentTemplate[];
}

export interface BrowserSession {
  access_token: string;
  expires_in: number;
  sample_rate: number;
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

export interface WorkflowNode {
  id: string;
  position: number;
  node_type: string;
  config: Record<string, unknown>;
  next_node_id: string | null;
}

export interface CallWorkflow {
  id: string;
  tenant_id: string;
  agent_id: string | null;
  name: string;
  description: string | null;
  is_active: boolean;
  trigger_type: string;
  config: Record<string, unknown> | null;
  nodes: WorkflowNode[];
}

export interface DncEntry {
  id: string;
  phone_number: string;
  reason: string | null;
  source: string | null;
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

class ApiClient {
  private token: string | null = null;

  setToken(token: string) {
    this.token = token;
    if (typeof window !== 'undefined') {
      localStorage.setItem('access_token', token);
    }
  }

  getToken(): string | null {
    if (!this.token && typeof window !== 'undefined') {
      this.token = localStorage.getItem('access_token');
    }
    return this.token;
  }

  private async request<T>(path: string, options: RequestInit = {}): Promise<T> {
    const token = this.getToken();
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...(options.headers as Record<string, string> || {}),
    };
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }

    const response = await fetch(`${API_URL}${path}`, {
      ...options,
      headers,
    });

    if (response.status === 401) {
      if (typeof window !== 'undefined') {
        localStorage.removeItem('access_token');
        // A centralized fetch client cannot use the router hook; force a clean auth reset.
        // eslint-disable-next-line @next/next/no-location-assign-relative-destination
        window.location.href = '/login';
      }
      throw new Error('Unauthorized');
    }

    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: 'Request failed' }));
      throw new Error(error.detail || 'Request failed');
    }

    if (response.status === 204) return undefined as T;
    return response.json();
  }

  // Auth
  async login(email: string, password: string) {
    const data = await this.request<{
      access_token: string;
      refresh_token: string;
      tenant_id: string;
      user_id: string;
      role: string;
    }>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    });
    this.setToken(data.access_token);
    if (typeof window !== 'undefined') {
      localStorage.setItem('refresh_token', data.refresh_token);
    }
    return data;
  }

  async register(data: {
    email: string;
    password: string;
    full_name: string;
    tenant_name: string;
    tenant_slug: string;
  }) {
    const result = await this.request<{
      access_token: string;
      refresh_token: string;
      tenant_id: string;
      user_id: string;
      role: string;
    }>('/api/v1/auth/register', {
      method: 'POST',
      body: JSON.stringify(data),
    });
    this.setToken(result.access_token);
    return result;
  }

  async getMe() {
    return this.request<{ id: string; email: string; full_name: string; role: string }>('/api/v1/auth/me');
  }

  // Agents
  async listAgents() {
    return this.request<VoiceAgent[]>('/api/v1/agents');
  }

  async createAgent(data: Partial<VoiceAgent> & { name: string; system_prompt: string }) {
    return this.request<VoiceAgent>('/api/v1/agents', { method: 'POST', body: JSON.stringify(data) });
  }

  async getAgent(id: string) {
    return this.request<VoiceAgent>(`/api/v1/agents/${id}`);
  }

  async updateAgent(id: string, data: Partial<VoiceAgent>) {
    return this.request<VoiceAgent>(`/api/v1/agents/${id}`, { method: 'PATCH', body: JSON.stringify(data) });
  }

  async deleteAgent(id: string) {
    return this.request<void>(`/api/v1/agents/${id}`, { method: 'DELETE' });
  }

  async getProviderStatus() {
    return this.request<ProviderStatus>('/api/v1/agents/provider/status');
  }

  async getAgentProviderCatalog() {
    return this.request<AgentProviderCatalog>('/api/v1/agents/provider/catalog');
  }

  async provisionSmallestAgent(id: string) {
    return this.request<VoiceAgent>(`/api/v1/agents/${id}/smallest/provision`, { method: 'POST' });
  }

  async syncSmallestAgent(id: string) {
    return this.request<VoiceAgent>(`/api/v1/agents/${id}/smallest/sync`, { method: 'POST' });
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

  // Calls
  async listCalls(params?: Record<string, string>) {
    const query = params ? '?' + new URLSearchParams(params).toString() : '';
    return this.request<CallRecord[]>(`/api/v1/calls${query}`);
  }

  async getCall(id: string) {
    return this.request<CallRecord>(`/api/v1/calls/${id}`);
  }

  async initiateCall(data: {
    agent_id: string;
    to_number: string;
    from_product_id?: string;
    version_id?: string;
    context?: Record<string, string | number | boolean>;
  }) {
    return this.request<CallRecord>('/api/v1/calls', { method: 'POST', body: JSON.stringify(data) });
  }

  async getCallTranscript(callId: string) {
    return this.request<CallTranscript>(`/api/v1/calls/${callId}/transcript`);
  }

  async getCallSummary(callId: string) {
    return this.request<CallSummary>(`/api/v1/calls/${callId}/summary`);
  }

  // Campaigns
  async listCampaigns() {
    return this.request<Campaign[]>('/api/v1/campaigns');
  }

  async createCampaign(data: {
    name: string;
    agent_id: string;
    max_concurrent_calls: number;
    calling_hours_start: string;
    calling_hours_end: string;
  }) {
    return this.request<Campaign>('/api/v1/campaigns', { method: 'POST', body: JSON.stringify(data) });
  }

  async startCampaign(id: string) {
    return this.request<Campaign>(`/api/v1/campaigns/${id}/start`, { method: 'POST' });
  }

  async pauseCampaign(id: string) {
    return this.request<Campaign>(`/api/v1/campaigns/${id}/pause`, { method: 'POST' });
  }

  // Analytics
  async getOverview(days = 30) {
    return this.request<AnalyticsOverview>(`/api/v1/analytics/overview?days=${days}`);
  }

  async getTimeseries(days = 30) {
    return this.request<AnalyticsTimeSeries>(`/api/v1/analytics/timeseries?days=${days}`);
  }

  // Workflows
  async listWorkflows() {
    return this.request<CallWorkflow[]>('/api/v1/workflows');
  }

  async createWorkflow(data: {
    name: string;
    trigger_type: string;
    agent_id?: string;
    description?: string;
    config?: Record<string, unknown>;
  }) {
    return this.request<CallWorkflow>('/api/v1/workflows', { method: 'POST', body: JSON.stringify(data) });
  }

  // Compliance
  async checkDnc(phone: string) {
    return this.request<{ is_on_dnc: boolean }>(`/api/v1/compliance/dnc/check?phone_number=${encodeURIComponent(phone)}`);
  }

  async addDnc(data: { phone_number: string; reason?: string }) {
    return this.request<DncEntry>('/api/v1/compliance/dnc', { method: 'POST', body: JSON.stringify(data) });
  }

  // Billing
  async getUsage(days = 30) {
    return this.request<UsageSummary>(`/api/v1/billing/usage?days=${days}`);
  }

  async getPlans() {
    return this.request<BillingPlan[]>('/api/v1/billing/plans');
  }
}

export const api = new ApiClient();
