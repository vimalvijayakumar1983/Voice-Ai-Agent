const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

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
    return this.request<any[]>('/api/v1/agents');
  }

  async createAgent(data: any) {
    return this.request<any>('/api/v1/agents', { method: 'POST', body: JSON.stringify(data) });
  }

  async getAgent(id: string) {
    return this.request<any>(`/api/v1/agents/${id}`);
  }

  async updateAgent(id: string, data: any) {
    return this.request<any>(`/api/v1/agents/${id}`, { method: 'PATCH', body: JSON.stringify(data) });
  }

  async deleteAgent(id: string) {
    return this.request<void>(`/api/v1/agents/${id}`, { method: 'DELETE' });
  }

  // Calls
  async listCalls(params?: Record<string, string>) {
    const query = params ? '?' + new URLSearchParams(params).toString() : '';
    return this.request<any[]>(`/api/v1/calls${query}`);
  }

  async getCall(id: string) {
    return this.request<any>(`/api/v1/calls/${id}`);
  }

  async initiateCall(data: { agent_id: string; to_number: string }) {
    return this.request<any>('/api/v1/calls', { method: 'POST', body: JSON.stringify(data) });
  }

  async getCallTranscript(callId: string) {
    return this.request<any>(`/api/v1/calls/${callId}/transcript`);
  }

  async getCallSummary(callId: string) {
    return this.request<any>(`/api/v1/calls/${callId}/summary`);
  }

  // Campaigns
  async listCampaigns() {
    return this.request<any[]>('/api/v1/campaigns');
  }

  async createCampaign(data: any) {
    return this.request<any>('/api/v1/campaigns', { method: 'POST', body: JSON.stringify(data) });
  }

  async startCampaign(id: string) {
    return this.request<any>(`/api/v1/campaigns/${id}/start`, { method: 'POST' });
  }

  async pauseCampaign(id: string) {
    return this.request<any>(`/api/v1/campaigns/${id}/pause`, { method: 'POST' });
  }

  // Analytics
  async getOverview(days = 30) {
    return this.request<any>(`/api/v1/analytics/overview?days=${days}`);
  }

  async getTimeseries(days = 30) {
    return this.request<any>(`/api/v1/analytics/timeseries?days=${days}`);
  }

  // Workflows
  async listWorkflows() {
    return this.request<any[]>('/api/v1/workflows');
  }

  async createWorkflow(data: any) {
    return this.request<any>('/api/v1/workflows', { method: 'POST', body: JSON.stringify(data) });
  }

  // Compliance
  async checkDnc(phone: string) {
    return this.request<{ is_on_dnc: boolean }>(`/api/v1/compliance/dnc/check?phone_number=${encodeURIComponent(phone)}`);
  }

  async addDnc(data: { phone_number: string; reason?: string }) {
    return this.request<any>('/api/v1/compliance/dnc', { method: 'POST', body: JSON.stringify(data) });
  }

  // Billing
  async getUsage(days = 30) {
    return this.request<any>(`/api/v1/billing/usage?days=${days}`);
  }

  async getPlans() {
    return this.request<any[]>('/api/v1/billing/plans');
  }
}

export const api = new ApiClient();
