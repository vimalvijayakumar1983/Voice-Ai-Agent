import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import {
  ICRMProvider,
  CRMProvider,
  CRMContact,
  CRMDeal,
  CRMAccount,
  CRMActivity,
  OAuthTokens,
  SyncError,
} from '../crm.interface';

// ---------- HubSpot API Types ----------

interface HubSpotPaginatedResponse<T> {
  results: T[];
  paging?: {
    next?: { after: string; link: string };
  };
  total?: number;
}

interface HubSpotObject {
  id: string;
  properties: Record<string, string | null>;
  createdAt: string;
  updatedAt: string;
  associations?: Record<string, { results: Array<{ id: string; type: string }> }>;
}

interface HubSpotBatchResult {
  status: string;
  results: HubSpotObject[];
  errors?: Array<{
    status: string;
    category: string;
    message: string;
    context?: Record<string, string[]>;
  }>;
}

// ---------- Provider ----------

@Injectable()
export class HubSpotProvider implements ICRMProvider {
  readonly providerName = CRMProvider.HUBSPOT;

  private readonly logger = new Logger(HubSpotProvider.name);
  private readonly clientId: string;
  private readonly clientSecret: string;
  private readonly redirectUri: string;
  private readonly baseUrl = 'https://api.hubapi.com';

  constructor(private readonly configService: ConfigService) {
    this.clientId = this.configService.get<string>('HUBSPOT_CLIENT_ID', '');
    this.clientSecret = this.configService.get<string>('HUBSPOT_CLIENT_SECRET', '');
    this.redirectUri = this.configService.get<string>('HUBSPOT_REDIRECT_URI', '');
  }

  // ---------- OAuth ----------

  getAuthorizationUrl(state: string): string {
    const scopes = [
      'crm.objects.contacts.read',
      'crm.objects.contacts.write',
      'crm.objects.deals.read',
      'crm.objects.deals.write',
      'crm.objects.companies.read',
      'crm.objects.companies.write',
    ];

    const params = new URLSearchParams({
      client_id: this.clientId,
      redirect_uri: this.redirectUri,
      scope: scopes.join(' '),
      state,
    });

    return `https://app.hubspot.com/oauth/authorize?${params.toString()}`;
  }

  async exchangeCodeForTokens(code: string): Promise<OAuthTokens> {
    const body = new URLSearchParams({
      grant_type: 'authorization_code',
      code,
      client_id: this.clientId,
      client_secret: this.clientSecret,
      redirect_uri: this.redirectUri,
    });

    const response = await fetch('https://api.hubapi.com/oauth/v1/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString(),
    });

    if (!response.ok) {
      const errorText = await response.text();
      this.logger.error(`HubSpot token exchange failed: ${errorText}`);
      throw new Error(`HubSpot OAuth token exchange failed: ${response.status}`);
    }

    const data = await response.json();

    return {
      accessToken: data.access_token,
      refreshToken: data.refresh_token,
      tokenType: 'Bearer',
      expiresAt: new Date(Date.now() + data.expires_in * 1000),
      scope: data.scope,
    };
  }

  async refreshAccessToken(refreshToken: string): Promise<OAuthTokens> {
    const body = new URLSearchParams({
      grant_type: 'refresh_token',
      refresh_token: refreshToken,
      client_id: this.clientId,
      client_secret: this.clientSecret,
    });

    const response = await fetch('https://api.hubapi.com/oauth/v1/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString(),
    });

    if (!response.ok) {
      const errorText = await response.text();
      this.logger.error(`HubSpot token refresh failed: ${errorText}`);
      throw new Error(`HubSpot token refresh failed: ${response.status}`);
    }

    const data = await response.json();

    return {
      accessToken: data.access_token,
      refreshToken: data.refresh_token,
      tokenType: 'Bearer',
      expiresAt: new Date(Date.now() + data.expires_in * 1000),
    };
  }

  // ---------- Contacts ----------

  async getContacts(
    tokens: OAuthTokens,
    since?: Date,
    limit = 100,
    offset = 0,
  ): Promise<{ contacts: CRMContact[]; hasMore: boolean; total?: number }> {
    const params = new URLSearchParams({
      limit: String(Math.min(limit, 100)),
      properties: 'firstname,lastname,email,phone,company,jobtitle,hs_lead_status,hubspot_owner_id,createdate,lastmodifieddate',
    });

    if (offset) {
      params.set('after', String(offset));
    }

    let url = `/crm/v3/objects/contacts?${params.toString()}`;

    // Use search endpoint for filtering by date
    if (since) {
      const searchBody = {
        filterGroups: [
          {
            filters: [
              {
                propertyName: 'lastmodifieddate',
                operator: 'GTE',
                value: since.getTime().toString(),
              },
            ],
          },
        ],
        sorts: [{ propertyName: 'lastmodifieddate', direction: 'DESCENDING' }],
        properties: ['firstname', 'lastname', 'email', 'phone', 'company', 'jobtitle', 'hs_lead_status', 'hubspot_owner_id'],
        limit: Math.min(limit, 100),
        after: offset || 0,
      };

      const result = await this.apiPost<HubSpotPaginatedResponse<HubSpotObject>>(
        tokens,
        '/crm/v3/objects/contacts/search',
        searchBody,
      );

      return {
        contacts: result.results.map((obj) => this.mapFromHubSpotContact(obj)),
        hasMore: !!result.paging?.next,
        total: result.total,
      };
    }

    const result = await this.apiGet<HubSpotPaginatedResponse<HubSpotObject>>(tokens, url);

    return {
      contacts: result.results.map((obj) => this.mapFromHubSpotContact(obj)),
      hasMore: !!result.paging?.next,
      total: result.total,
    };
  }

  async getContactById(tokens: OAuthTokens, externalId: string): Promise<CRMContact> {
    const result = await this.apiGet<HubSpotObject>(
      tokens,
      `/crm/v3/objects/contacts/${externalId}?properties=firstname,lastname,email,phone,company,jobtitle,hs_lead_status,hubspot_owner_id`,
    );

    return this.mapFromHubSpotContact(result);
  }

  async createContact(tokens: OAuthTokens, contact: CRMContact): Promise<CRMContact> {
    const properties = this.mapToHubSpotContactProperties(contact);

    const result = await this.apiPost<HubSpotObject>(
      tokens,
      '/crm/v3/objects/contacts',
      { properties },
    );

    return { ...contact, externalId: result.id };
  }

  async updateContact(
    tokens: OAuthTokens,
    externalId: string,
    contact: Partial<CRMContact>,
  ): Promise<CRMContact> {
    const properties = this.mapToHubSpotContactProperties(contact);

    const result = await this.apiPatch<HubSpotObject>(
      tokens,
      `/crm/v3/objects/contacts/${externalId}`,
      { properties },
    );

    return this.mapFromHubSpotContact(result);
  }

  async upsertContact(
    tokens: OAuthTokens,
    contact: CRMContact,
    matchField: string,
  ): Promise<CRMContact> {
    // HubSpot uses email as the default dedup key
    // Try to find existing contact first
    if (contact.email) {
      try {
        const searchResult = await this.apiPost<HubSpotPaginatedResponse<HubSpotObject>>(
          tokens,
          '/crm/v3/objects/contacts/search',
          {
            filterGroups: [
              {
                filters: [
                  {
                    propertyName: matchField === 'Email' ? 'email' : matchField,
                    operator: 'EQ',
                    value: contact.email,
                  },
                ],
              },
            ],
            limit: 1,
          },
        );

        if (searchResult.results.length > 0) {
          return this.updateContact(tokens, searchResult.results[0].id, contact);
        }
      } catch {
        // Contact not found, create new
      }
    }

    return this.createContact(tokens, contact);
  }

  async deleteContact(tokens: OAuthTokens, externalId: string): Promise<void> {
    await this.apiDelete(tokens, `/crm/v3/objects/contacts/${externalId}`);
  }

  // ---------- Deals ----------

  async getDeals(
    tokens: OAuthTokens,
    since?: Date,
    limit = 100,
    offset = 0,
  ): Promise<{ deals: CRMDeal[]; hasMore: boolean }> {
    const properties = 'dealname,amount,dealstage,closedate,pipeline,hubspot_owner_id,hs_lastmodifieddate';

    if (since) {
      const searchBody = {
        filterGroups: [
          {
            filters: [
              {
                propertyName: 'hs_lastmodifieddate',
                operator: 'GTE',
                value: since.getTime().toString(),
              },
            ],
          },
        ],
        properties: properties.split(','),
        limit: Math.min(limit, 100),
        after: offset || 0,
      };

      const result = await this.apiPost<HubSpotPaginatedResponse<HubSpotObject>>(
        tokens,
        '/crm/v3/objects/deals/search',
        searchBody,
      );

      return {
        deals: result.results.map((obj) => this.mapFromHubSpotDeal(obj)),
        hasMore: !!result.paging?.next,
      };
    }

    const params = new URLSearchParams({
      limit: String(Math.min(limit, 100)),
      properties,
    });
    if (offset) params.set('after', String(offset));

    const result = await this.apiGet<HubSpotPaginatedResponse<HubSpotObject>>(
      tokens,
      `/crm/v3/objects/deals?${params.toString()}`,
    );

    return {
      deals: result.results.map((obj) => this.mapFromHubSpotDeal(obj)),
      hasMore: !!result.paging?.next,
    };
  }

  async createDeal(tokens: OAuthTokens, deal: CRMDeal): Promise<CRMDeal> {
    const properties: Record<string, string> = {
      dealname: deal.name,
      dealstage: deal.stage,
    };

    if (deal.amount !== undefined) properties.amount = String(deal.amount);
    if (deal.closeDate) properties.closedate = deal.closeDate;
    if (deal.ownerId) properties.hubspot_owner_id = deal.ownerId;

    const result = await this.apiPost<HubSpotObject>(
      tokens,
      '/crm/v3/objects/deals',
      { properties },
    );

    // Create association with contact if provided
    if (deal.contactId) {
      await this.createAssociation(tokens, 'deals', result.id, 'contacts', deal.contactId, 'deal_to_contact');
    }

    return { ...deal, externalId: result.id };
  }

  async updateDeal(
    tokens: OAuthTokens,
    externalId: string,
    deal: Partial<CRMDeal>,
  ): Promise<CRMDeal> {
    const properties: Record<string, string> = {};
    if (deal.name) properties.dealname = deal.name;
    if (deal.stage) properties.dealstage = deal.stage;
    if (deal.amount !== undefined) properties.amount = String(deal.amount);
    if (deal.closeDate) properties.closedate = deal.closeDate;

    const result = await this.apiPatch<HubSpotObject>(
      tokens,
      `/crm/v3/objects/deals/${externalId}`,
      { properties },
    );

    return this.mapFromHubSpotDeal(result);
  }

  // ---------- Activities (Engagements) ----------

  async logActivity(tokens: OAuthTokens, activity: CRMActivity): Promise<CRMActivity> {
    // HubSpot uses Engagements API for call logging
    if (activity.type === 'call') {
      return this.logCallEngagement(tokens, activity);
    }

    // For other activity types, use Notes
    const properties: Record<string, string> = {
      hs_note_body: `${activity.subject}\n\n${activity.description || ''}`,
      hs_timestamp: new Date(activity.completedAt || Date.now()).getTime().toString(),
    };

    const result = await this.apiPost<HubSpotObject>(
      tokens,
      '/crm/v3/objects/notes',
      { properties },
    );

    // Associate with contact
    if (activity.contactId) {
      await this.createAssociation(tokens, 'notes', result.id, 'contacts', activity.contactId, 'note_to_contact');
    }

    return { ...activity, externalId: result.id };
  }

  private async logCallEngagement(
    tokens: OAuthTokens,
    activity: CRMActivity,
  ): Promise<CRMActivity> {
    const properties: Record<string, string> = {
      hs_call_title: activity.subject,
      hs_call_body: activity.description || '',
      hs_call_status: activity.outcome === 'connected' ? 'COMPLETED' : 'NO_ANSWER',
      hs_call_duration: String((activity.duration || 0) * 1000), // HubSpot expects milliseconds
      hs_call_direction: activity.callDirection === 'inbound' ? 'INBOUND' : 'OUTBOUND',
      hs_timestamp: new Date(activity.completedAt || Date.now()).getTime().toString(),
    };

    if (activity.recordingUrl) {
      properties.hs_call_recording_url = activity.recordingUrl;
    }

    const result = await this.apiPost<HubSpotObject>(
      tokens,
      '/crm/v3/objects/calls',
      { properties },
    );

    // Associate call with contact
    if (activity.contactId) {
      await this.createAssociation(tokens, 'calls', result.id, 'contacts', activity.contactId, 'call_to_contact');
    }

    // Associate call with deal
    if (activity.dealId) {
      await this.createAssociation(tokens, 'calls', result.id, 'deals', activity.dealId, 'call_to_deal');
    }

    return { ...activity, externalId: result.id };
  }

  async getActivities(
    tokens: OAuthTokens,
    contactId: string,
    since?: Date,
  ): Promise<CRMActivity[]> {
    // Get calls associated with the contact
    const associationsResult = await this.apiGet<{
      results: Array<{ id: string; type: string }>;
    }>(
      tokens,
      `/crm/v3/objects/contacts/${contactId}/associations/calls`,
    );

    if (!associationsResult.results.length) return [];

    const callIds = associationsResult.results.map((r) => r.id).slice(0, 50);

    const batchResult = await this.apiPost<{ results: HubSpotObject[] }>(
      tokens,
      '/crm/v3/objects/calls/batch/read',
      {
        inputs: callIds.map((id) => ({ id })),
        properties: ['hs_call_title', 'hs_call_body', 'hs_call_status', 'hs_call_duration', 'hs_call_direction', 'hs_call_recording_url', 'hs_timestamp'],
      },
    );

    return batchResult.results
      .filter((obj) => {
        if (!since) return true;
        return new Date(obj.createdAt) >= since;
      })
      .map((obj) => ({
        externalId: obj.id,
        type: 'call' as const,
        subject: obj.properties.hs_call_title || 'Call',
        description: obj.properties.hs_call_body || undefined,
        status: obj.properties.hs_call_status || undefined,
        contactId,
        duration: obj.properties.hs_call_duration
          ? parseInt(obj.properties.hs_call_duration) / 1000
          : undefined,
        callDirection: obj.properties.hs_call_direction === 'INBOUND'
          ? 'inbound' as const
          : 'outbound' as const,
        recordingUrl: obj.properties.hs_call_recording_url || undefined,
        completedAt: obj.properties.hs_timestamp
          ? new Date(parseInt(obj.properties.hs_timestamp)).toISOString()
          : obj.createdAt,
      }));
  }

  // ---------- Accounts (Companies) ----------

  async getAccounts(
    tokens: OAuthTokens,
    since?: Date,
    limit = 100,
    offset = 0,
  ): Promise<{ accounts: CRMAccount[]; hasMore: boolean }> {
    const properties = 'name,website,industry,phone,address,city,state,zip,country,hubspot_owner_id';

    const params = new URLSearchParams({
      limit: String(Math.min(limit, 100)),
      properties,
    });
    if (offset) params.set('after', String(offset));

    const result = await this.apiGet<HubSpotPaginatedResponse<HubSpotObject>>(
      tokens,
      `/crm/v3/objects/companies?${params.toString()}`,
    );

    const accounts: CRMAccount[] = result.results
      .filter((obj) => {
        if (!since) return true;
        return new Date(obj.updatedAt) >= since;
      })
      .map((obj) => ({
        externalId: obj.id,
        name: obj.properties.name || '',
        website: obj.properties.website || undefined,
        industry: obj.properties.industry || undefined,
        phone: obj.properties.phone || undefined,
        address: {
          street: obj.properties.address || undefined,
          city: obj.properties.city || undefined,
          state: obj.properties.state || undefined,
          postalCode: obj.properties.zip || undefined,
          country: obj.properties.country || undefined,
        },
        ownerId: obj.properties.hubspot_owner_id || undefined,
        createdAt: obj.createdAt,
        updatedAt: obj.updatedAt,
      }));

    return { accounts, hasMore: !!result.paging?.next };
  }

  async createAccount(tokens: OAuthTokens, account: CRMAccount): Promise<CRMAccount> {
    const properties: Record<string, string> = {
      name: account.name,
    };

    if (account.website) properties.website = account.website;
    if (account.industry) properties.industry = account.industry;
    if (account.phone) properties.phone = account.phone;
    if (account.address?.street) properties.address = account.address.street;
    if (account.address?.city) properties.city = account.address.city;
    if (account.address?.state) properties.state = account.address.state;
    if (account.address?.postalCode) properties.zip = account.address.postalCode;
    if (account.address?.country) properties.country = account.address.country;

    const result = await this.apiPost<HubSpotObject>(
      tokens,
      '/crm/v3/objects/companies',
      { properties },
    );

    return { ...account, externalId: result.id };
  }

  // ---------- Bulk Operations ----------

  async bulkCreateContacts(
    tokens: OAuthTokens,
    contacts: CRMContact[],
  ): Promise<{ successes: CRMContact[]; failures: SyncError[] }> {
    const successes: CRMContact[] = [];
    const failures: SyncError[] = [];
    const batchSize = 100;

    for (let i = 0; i < contacts.length; i += batchSize) {
      const batch = contacts.slice(i, i + batchSize);

      try {
        const result = await this.apiPost<HubSpotBatchResult>(
          tokens,
          '/crm/v3/objects/contacts/batch/create',
          {
            inputs: batch.map((contact) => ({
              properties: this.mapToHubSpotContactProperties(contact),
            })),
          },
        );

        for (const obj of result.results) {
          successes.push(this.mapFromHubSpotContact(obj));
        }

        if (result.errors) {
          for (const error of result.errors) {
            failures.push({
              operation: 'create',
              message: error.message,
              details: error.context,
            });
          }
        }
      } catch (error) {
        for (const contact of batch) {
          failures.push({
            entityId: contact.id,
            operation: 'create',
            message: (error as Error).message,
          });
        }
      }
    }

    return { successes, failures };
  }

  async bulkUpdateContacts(
    tokens: OAuthTokens,
    contacts: Array<{ externalId: string; data: Partial<CRMContact> }>,
  ): Promise<{ successes: CRMContact[]; failures: SyncError[] }> {
    const successes: CRMContact[] = [];
    const failures: SyncError[] = [];
    const batchSize = 100;

    for (let i = 0; i < contacts.length; i += batchSize) {
      const batch = contacts.slice(i, i + batchSize);

      try {
        const result = await this.apiPost<HubSpotBatchResult>(
          tokens,
          '/crm/v3/objects/contacts/batch/update',
          {
            inputs: batch.map(({ externalId, data }) => ({
              id: externalId,
              properties: this.mapToHubSpotContactProperties(data),
            })),
          },
        );

        for (const obj of result.results) {
          successes.push(this.mapFromHubSpotContact(obj));
        }

        if (result.errors) {
          for (const error of result.errors) {
            failures.push({
              operation: 'update',
              message: error.message,
              details: error.context,
            });
          }
        }
      } catch (error) {
        for (const item of batch) {
          failures.push({
            externalId: item.externalId,
            operation: 'update',
            message: (error as Error).message,
          });
        }
      }
    }

    return { successes, failures };
  }

  // ---------- Associations ----------

  private async createAssociation(
    tokens: OAuthTokens,
    fromObjectType: string,
    fromObjectId: string,
    toObjectType: string,
    toObjectId: string,
    associationType: string,
  ): Promise<void> {
    try {
      await this.apiPut(
        tokens,
        `/crm/v3/objects/${fromObjectType}/${fromObjectId}/associations/${toObjectType}/${toObjectId}/${associationType}`,
        {},
      );
    } catch (error) {
      this.logger.warn(
        `Failed to create association ${fromObjectType}/${fromObjectId} -> ${toObjectType}/${toObjectId}: ${(error as Error).message}`,
      );
    }
  }

  // ---------- Schema ----------

  async getAvailableFields(
    tokens: OAuthTokens,
    objectType: string,
  ): Promise<Array<{ name: string; label: string; type: string; required: boolean }>> {
    const result = await this.apiGet<{ results: Array<{ name: string; label: string; type: string; fieldType: string; hasUniqueValue: boolean }> }>(
      tokens,
      `/crm/v3/properties/${objectType}`,
    );

    return result.results.map((prop) => ({
      name: prop.name,
      label: prop.label,
      type: prop.type,
      required: prop.hasUniqueValue,
    }));
  }

  // ---------- Health ----------

  async testConnection(tokens: OAuthTokens): Promise<boolean> {
    try {
      await this.apiGet(tokens, '/crm/v3/objects/contacts?limit=1');
      return true;
    } catch {
      return false;
    }
  }

  // ---------- HTTP Helpers ----------

  private async apiGet<T>(tokens: OAuthTokens, path: string): Promise<T> {
    return this.apiRequest<T>(tokens, 'GET', path);
  }

  private async apiPost<T>(tokens: OAuthTokens, path: string, body: unknown): Promise<T> {
    return this.apiRequest<T>(tokens, 'POST', path, body);
  }

  private async apiPatch<T>(tokens: OAuthTokens, path: string, body: unknown): Promise<T> {
    return this.apiRequest<T>(tokens, 'PATCH', path, body);
  }

  private async apiPut<T>(tokens: OAuthTokens, path: string, body: unknown): Promise<T> {
    return this.apiRequest<T>(tokens, 'PUT', path, body);
  }

  private async apiDelete(tokens: OAuthTokens, path: string): Promise<void> {
    await this.apiRequest<void>(tokens, 'DELETE', path);
  }

  private async apiRequest<T>(
    tokens: OAuthTokens,
    method: string,
    path: string,
    body?: unknown,
    retryCount = 0,
  ): Promise<T> {
    const url = `${this.baseUrl}${path}`;

    const headers: Record<string, string> = {
      Authorization: `Bearer ${tokens.accessToken}`,
      'Content-Type': 'application/json',
    };

    const options: RequestInit = { method, headers };
    if (body && method !== 'GET') {
      options.body = JSON.stringify(body);
    }

    const response = await fetch(url, options);

    // Handle 429 rate limiting
    if (response.status === 429 && retryCount < 3) {
      const retryAfter = parseInt(response.headers.get('Retry-After') || '10');
      this.logger.warn(`HubSpot rate limited, retrying in ${retryAfter}s`);
      await new Promise((resolve) => setTimeout(resolve, retryAfter * 1000));
      return this.apiRequest<T>(tokens, method, path, body, retryCount + 1);
    }

    if (response.status === 204) {
      return undefined as unknown as T;
    }

    if (!response.ok) {
      const errorBody = await response.text();
      this.logger.error(
        `HubSpot API error: ${method} ${path} - ${response.status} ${errorBody}`,
      );
      throw new Error(`HubSpot API error: ${response.status} ${response.statusText}`);
    }

    const text = await response.text();
    return text ? JSON.parse(text) : (undefined as unknown as T);
  }

  // ---------- Mapping Helpers ----------

  private mapFromHubSpotContact(obj: HubSpotObject): CRMContact {
    return {
      externalId: obj.id,
      firstName: obj.properties.firstname || '',
      lastName: obj.properties.lastname || '',
      email: obj.properties.email || undefined,
      phone: obj.properties.phone || undefined,
      company: obj.properties.company || undefined,
      title: obj.properties.jobtitle || undefined,
      source: obj.properties.hs_lead_status || undefined,
      ownerId: obj.properties.hubspot_owner_id || undefined,
      createdAt: obj.createdAt,
      updatedAt: obj.updatedAt,
    };
  }

  private mapFromHubSpotDeal(obj: HubSpotObject): CRMDeal {
    return {
      externalId: obj.id,
      name: obj.properties.dealname || '',
      amount: obj.properties.amount ? parseFloat(obj.properties.amount) : undefined,
      stage: obj.properties.dealstage || '',
      closeDate: obj.properties.closedate || undefined,
      ownerId: obj.properties.hubspot_owner_id || undefined,
      createdAt: obj.createdAt,
      updatedAt: obj.updatedAt,
    };
  }

  private mapToHubSpotContactProperties(contact: Partial<CRMContact>): Record<string, string> {
    const properties: Record<string, string> = {};

    if (contact.firstName !== undefined) properties.firstname = contact.firstName;
    if (contact.lastName !== undefined) properties.lastname = contact.lastName;
    if (contact.email !== undefined) properties.email = contact.email;
    if (contact.phone !== undefined) properties.phone = contact.phone;
    if (contact.company !== undefined) properties.company = contact.company;
    if (contact.title !== undefined) properties.jobtitle = contact.title;

    if (contact.customFields) {
      for (const [key, value] of Object.entries(contact.customFields)) {
        if (value !== undefined && value !== null) {
          properties[key] = String(value);
        }
      }
    }

    return properties;
  }
}
