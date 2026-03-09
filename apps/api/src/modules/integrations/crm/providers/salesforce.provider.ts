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

// ---------- Salesforce API Types ----------

interface SalesforceQueryResult<T> {
  totalSize: number;
  done: boolean;
  nextRecordsUrl?: string;
  records: T[];
}

interface SalesforceRecord {
  Id: string;
  attributes?: { type: string; url: string };
  [key: string]: unknown;
}

interface SalesforceError {
  message: string;
  errorCode: string;
  fields?: string[];
}

interface SalesforceBulkResult {
  id: string;
  success: boolean;
  errors: SalesforceError[];
}

// ---------- Rate Limiting ----------

interface RateLimitState {
  remaining: number;
  resetAt: number;
}

// ---------- Provider ----------

@Injectable()
export class SalesforceProvider implements ICRMProvider {
  readonly providerName = CRMProvider.SALESFORCE;

  private readonly logger = new Logger(SalesforceProvider.name);
  private readonly clientId: string;
  private readonly clientSecret: string;
  private readonly redirectUri: string;
  private readonly apiVersion: string;
  private readonly loginUrl: string;
  private rateLimitState: RateLimitState = { remaining: 100, resetAt: 0 };

  constructor(private readonly configService: ConfigService) {
    this.clientId = this.configService.get<string>('SALESFORCE_CLIENT_ID', '');
    this.clientSecret = this.configService.get<string>('SALESFORCE_CLIENT_SECRET', '');
    this.redirectUri = this.configService.get<string>('SALESFORCE_REDIRECT_URI', '');
    this.apiVersion = this.configService.get<string>('SALESFORCE_API_VERSION', 'v59.0');
    this.loginUrl = this.configService.get<string>(
      'SALESFORCE_LOGIN_URL',
      'https://login.salesforce.com',
    );
  }

  // ---------- OAuth ----------

  getAuthorizationUrl(state: string): string {
    const params = new URLSearchParams({
      response_type: 'code',
      client_id: this.clientId,
      redirect_uri: this.redirectUri,
      scope: 'api refresh_token offline_access',
      state,
      prompt: 'consent',
    });

    return `${this.loginUrl}/services/oauth2/authorize?${params.toString()}`;
  }

  async exchangeCodeForTokens(code: string): Promise<OAuthTokens> {
    const body = new URLSearchParams({
      grant_type: 'authorization_code',
      code,
      client_id: this.clientId,
      client_secret: this.clientSecret,
      redirect_uri: this.redirectUri,
    });

    const response = await fetch(`${this.loginUrl}/services/oauth2/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString(),
    });

    if (!response.ok) {
      const errorText = await response.text();
      this.logger.error(`Salesforce token exchange failed: ${errorText}`);
      throw new Error(`Salesforce OAuth token exchange failed: ${response.status}`);
    }

    const data = await response.json();

    return {
      accessToken: data.access_token,
      refreshToken: data.refresh_token,
      tokenType: data.token_type || 'Bearer',
      instanceUrl: data.instance_url,
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

    const response = await fetch(`${this.loginUrl}/services/oauth2/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString(),
    });

    if (!response.ok) {
      const errorText = await response.text();
      this.logger.error(`Salesforce token refresh failed: ${errorText}`);
      throw new Error(`Salesforce token refresh failed: ${response.status}`);
    }

    const data = await response.json();

    return {
      accessToken: data.access_token,
      refreshToken: refreshToken, // Salesforce may not return a new refresh token
      tokenType: data.token_type || 'Bearer',
      instanceUrl: data.instance_url,
      scope: data.scope,
    };
  }

  // ---------- Contacts ----------

  async getContacts(
    tokens: OAuthTokens,
    since?: Date,
    limit = 200,
    offset = 0,
  ): Promise<{ contacts: CRMContact[]; hasMore: boolean; total?: number }> {
    let soql = `SELECT Id, FirstName, LastName, Email, Phone, Account.Name, Title, LeadSource, OwnerId, CreatedDate, LastModifiedDate FROM Contact`;

    if (since) {
      soql += ` WHERE LastModifiedDate > ${since.toISOString()}`;
    }

    soql += ` ORDER BY LastModifiedDate DESC LIMIT ${limit} OFFSET ${offset}`;

    const result = await this.soqlQuery<SalesforceRecord>(tokens, soql);

    const contacts: CRMContact[] = result.records.map((rec) => ({
      externalId: rec.Id,
      firstName: (rec.FirstName as string) || '',
      lastName: (rec.LastName as string) || '',
      email: rec.Email as string | undefined,
      phone: rec.Phone as string | undefined,
      company: (rec.Account as any)?.Name as string | undefined,
      title: rec.Title as string | undefined,
      source: rec.LeadSource as string | undefined,
      ownerId: rec.OwnerId as string | undefined,
      createdAt: rec.CreatedDate as string | undefined,
      updatedAt: rec.LastModifiedDate as string | undefined,
    }));

    return {
      contacts,
      hasMore: !result.done,
      total: result.totalSize,
    };
  }

  async getContactById(tokens: OAuthTokens, externalId: string): Promise<CRMContact> {
    const rec = await this.apiGet<SalesforceRecord>(
      tokens,
      `/sobjects/Contact/${externalId}`,
    );

    return {
      externalId: rec.Id,
      firstName: (rec.FirstName as string) || '',
      lastName: (rec.LastName as string) || '',
      email: rec.Email as string | undefined,
      phone: rec.Phone as string | undefined,
      title: rec.Title as string | undefined,
      ownerId: rec.OwnerId as string | undefined,
    };
  }

  async createContact(tokens: OAuthTokens, contact: CRMContact): Promise<CRMContact> {
    const sfContact = this.mapToSalesforceContact(contact);

    const result = await this.apiPost<{ id: string; success: boolean }>(
      tokens,
      '/sobjects/Contact',
      sfContact,
    );

    return { ...contact, externalId: result.id };
  }

  async updateContact(
    tokens: OAuthTokens,
    externalId: string,
    contact: Partial<CRMContact>,
  ): Promise<CRMContact> {
    const sfContact = this.mapToSalesforceContact(contact);
    delete sfContact.Id;

    await this.apiPatch(tokens, `/sobjects/Contact/${externalId}`, sfContact);

    return { ...contact, externalId } as CRMContact;
  }

  async upsertContact(
    tokens: OAuthTokens,
    contact: CRMContact,
    matchField: string,
  ): Promise<CRMContact> {
    const sfContact = this.mapToSalesforceContact(contact);
    const matchValue = (sfContact as any)[matchField] || contact.email;

    const result = await this.apiPatch<{ id: string; success: boolean; created: boolean }>(
      tokens,
      `/sobjects/Contact/${matchField}/${encodeURIComponent(matchValue as string)}`,
      sfContact,
    );

    return { ...contact, externalId: result?.id || contact.externalId };
  }

  async deleteContact(tokens: OAuthTokens, externalId: string): Promise<void> {
    await this.apiDelete(tokens, `/sobjects/Contact/${externalId}`);
  }

  // ---------- Deals (Opportunities) ----------

  async getDeals(
    tokens: OAuthTokens,
    since?: Date,
    limit = 200,
    offset = 0,
  ): Promise<{ deals: CRMDeal[]; hasMore: boolean }> {
    let soql = `SELECT Id, Name, Amount, CurrencyIsoCode, StageName, Probability, ContactId, AccountId, CloseDate, OwnerId, CreatedDate, LastModifiedDate FROM Opportunity`;

    if (since) {
      soql += ` WHERE LastModifiedDate > ${since.toISOString()}`;
    }

    soql += ` ORDER BY LastModifiedDate DESC LIMIT ${limit} OFFSET ${offset}`;

    const result = await this.soqlQuery<SalesforceRecord>(tokens, soql);

    const deals: CRMDeal[] = result.records.map((rec) => ({
      externalId: rec.Id,
      name: (rec.Name as string) || '',
      amount: rec.Amount as number | undefined,
      currency: rec.CurrencyIsoCode as string | undefined,
      stage: (rec.StageName as string) || '',
      probability: rec.Probability as number | undefined,
      contactId: rec.ContactId as string | undefined,
      accountId: rec.AccountId as string | undefined,
      closeDate: rec.CloseDate as string | undefined,
      ownerId: rec.OwnerId as string | undefined,
      createdAt: rec.CreatedDate as string | undefined,
      updatedAt: rec.LastModifiedDate as string | undefined,
    }));

    return { deals, hasMore: !result.done };
  }

  async createDeal(tokens: OAuthTokens, deal: CRMDeal): Promise<CRMDeal> {
    const sfOpportunity: Record<string, unknown> = {
      Name: deal.name,
      Amount: deal.amount,
      StageName: deal.stage,
      Probability: deal.probability,
      CloseDate: deal.closeDate || new Date(Date.now() + 30 * 86400000).toISOString().split('T')[0],
      AccountId: deal.accountId,
      OwnerId: deal.ownerId,
    };

    const result = await this.apiPost<{ id: string }>(
      tokens,
      '/sobjects/Opportunity',
      sfOpportunity,
    );

    return { ...deal, externalId: result.id };
  }

  async updateDeal(
    tokens: OAuthTokens,
    externalId: string,
    deal: Partial<CRMDeal>,
  ): Promise<CRMDeal> {
    const sfData: Record<string, unknown> = {};
    if (deal.name) sfData.Name = deal.name;
    if (deal.amount !== undefined) sfData.Amount = deal.amount;
    if (deal.stage) sfData.StageName = deal.stage;
    if (deal.probability !== undefined) sfData.Probability = deal.probability;
    if (deal.closeDate) sfData.CloseDate = deal.closeDate;

    await this.apiPatch(tokens, `/sobjects/Opportunity/${externalId}`, sfData);

    return { ...deal, externalId } as CRMDeal;
  }

  // ---------- Activities (Tasks) ----------

  async logActivity(tokens: OAuthTokens, activity: CRMActivity): Promise<CRMActivity> {
    const sfTask: Record<string, unknown> = {
      Subject: activity.subject,
      Description: activity.description,
      Status: activity.status || 'Completed',
      Priority: 'Normal',
      TaskSubtype: activity.type === 'call' ? 'Call' : 'Task',
      WhoId: activity.contactId,
      WhatId: activity.dealId,
      CallDurationInSeconds: activity.duration,
      CallDisposition: activity.outcome,
      CallType: activity.callDirection === 'inbound' ? 'Inbound' : 'Outbound',
      ActivityDate: activity.completedAt
        ? new Date(activity.completedAt).toISOString().split('T')[0]
        : new Date().toISOString().split('T')[0],
    };

    if (activity.recordingUrl) {
      sfTask.Description = `${activity.description || ''}\n\nRecording: ${activity.recordingUrl}`;
    }

    const result = await this.apiPost<{ id: string }>(tokens, '/sobjects/Task', sfTask);

    return { ...activity, externalId: result.id };
  }

  async getActivities(
    tokens: OAuthTokens,
    contactId: string,
    since?: Date,
  ): Promise<CRMActivity[]> {
    let soql = `SELECT Id, Subject, Description, Status, TaskSubtype, WhoId, WhatId, CallDurationInSeconds, CallDisposition, CallType, ActivityDate, CreatedDate FROM Task WHERE WhoId = '${contactId}'`;

    if (since) {
      soql += ` AND CreatedDate > ${since.toISOString()}`;
    }

    soql += ` ORDER BY CreatedDate DESC LIMIT 50`;

    const result = await this.soqlQuery<SalesforceRecord>(tokens, soql);

    return result.records.map((rec) => ({
      externalId: rec.Id,
      type: rec.TaskSubtype === 'Call' ? 'call' as const : 'task' as const,
      subject: (rec.Subject as string) || '',
      description: rec.Description as string | undefined,
      status: rec.Status as string | undefined,
      contactId: rec.WhoId as string | undefined,
      dealId: rec.WhatId as string | undefined,
      duration: rec.CallDurationInSeconds as number | undefined,
      outcome: rec.CallDisposition as string | undefined,
      callDirection: rec.CallType === 'Inbound' ? 'inbound' as const : 'outbound' as const,
      completedAt: rec.ActivityDate as string | undefined,
    }));
  }

  // ---------- Accounts ----------

  async getAccounts(
    tokens: OAuthTokens,
    since?: Date,
    limit = 200,
    offset = 0,
  ): Promise<{ accounts: CRMAccount[]; hasMore: boolean }> {
    let soql = `SELECT Id, Name, Website, Industry, Phone, BillingStreet, BillingCity, BillingState, BillingPostalCode, BillingCountry, OwnerId, CreatedDate, LastModifiedDate FROM Account`;

    if (since) {
      soql += ` WHERE LastModifiedDate > ${since.toISOString()}`;
    }

    soql += ` ORDER BY LastModifiedDate DESC LIMIT ${limit} OFFSET ${offset}`;

    const result = await this.soqlQuery<SalesforceRecord>(tokens, soql);

    const accounts: CRMAccount[] = result.records.map((rec) => ({
      externalId: rec.Id,
      name: (rec.Name as string) || '',
      website: rec.Website as string | undefined,
      industry: rec.Industry as string | undefined,
      phone: rec.Phone as string | undefined,
      address: {
        street: rec.BillingStreet as string | undefined,
        city: rec.BillingCity as string | undefined,
        state: rec.BillingState as string | undefined,
        postalCode: rec.BillingPostalCode as string | undefined,
        country: rec.BillingCountry as string | undefined,
      },
      ownerId: rec.OwnerId as string | undefined,
      createdAt: rec.CreatedDate as string | undefined,
      updatedAt: rec.LastModifiedDate as string | undefined,
    }));

    return { accounts, hasMore: !result.done };
  }

  async createAccount(tokens: OAuthTokens, account: CRMAccount): Promise<CRMAccount> {
    const sfAccount: Record<string, unknown> = {
      Name: account.name,
      Website: account.website,
      Industry: account.industry,
      Phone: account.phone,
      BillingStreet: account.address?.street,
      BillingCity: account.address?.city,
      BillingState: account.address?.state,
      BillingPostalCode: account.address?.postalCode,
      BillingCountry: account.address?.country,
    };

    const result = await this.apiPost<{ id: string }>(tokens, '/sobjects/Account', sfAccount);

    return { ...account, externalId: result.id };
  }

  // ---------- Bulk Operations ----------

  async bulkCreateContacts(
    tokens: OAuthTokens,
    contacts: CRMContact[],
  ): Promise<{ successes: CRMContact[]; failures: SyncError[] }> {
    const successes: CRMContact[] = [];
    const failures: SyncError[] = [];

    // Salesforce Composite API: up to 200 records
    const batchSize = 200;

    for (let i = 0; i < contacts.length; i += batchSize) {
      const batch = contacts.slice(i, i + batchSize);

      const compositeRequest = {
        allOrNone: false,
        records: batch.map((contact) => ({
          attributes: { type: 'Contact' },
          ...this.mapToSalesforceContact(contact),
        })),
      };

      try {
        const results = await this.apiPost<SalesforceBulkResult[]>(
          tokens,
          '/composite/sobjects',
          compositeRequest,
        );

        for (let j = 0; j < results.length; j++) {
          if (results[j].success) {
            successes.push({ ...batch[j], externalId: results[j].id });
          } else {
            failures.push({
              entityId: batch[j].id,
              operation: 'create',
              message: results[j].errors?.map((e) => e.message).join('; ') || 'Unknown error',
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

    const batchSize = 200;

    for (let i = 0; i < contacts.length; i += batchSize) {
      const batch = contacts.slice(i, i + batchSize);

      const compositeRequest = {
        allOrNone: false,
        records: batch.map(({ externalId, data }) => ({
          attributes: { type: 'Contact' },
          Id: externalId,
          ...this.mapToSalesforceContact(data),
        })),
      };

      try {
        const results = await this.apiPatch<SalesforceBulkResult[]>(
          tokens,
          '/composite/sobjects',
          compositeRequest,
        );

        if (Array.isArray(results)) {
          for (let j = 0; j < results.length; j++) {
            if (results[j].success) {
              successes.push({ ...batch[j].data, externalId: batch[j].externalId } as CRMContact);
            } else {
              failures.push({
                externalId: batch[j].externalId,
                operation: 'update',
                message: results[j].errors?.map((e) => e.message).join('; ') || 'Unknown error',
              });
            }
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

  // ---------- Schema ----------

  async getAvailableFields(
    tokens: OAuthTokens,
    objectType: string,
  ): Promise<Array<{ name: string; label: string; type: string; required: boolean }>> {
    const result = await this.apiGet<{ fields: Array<{ name: string; label: string; type: string; nillable: boolean; createable: boolean }> }>(
      tokens,
      `/sobjects/${objectType}/describe`,
    );

    return result.fields
      .filter((f) => f.createable)
      .map((f) => ({
        name: f.name,
        label: f.label,
        type: f.type.toLowerCase(),
        required: !f.nillable,
      }));
  }

  // ---------- Health ----------

  async testConnection(tokens: OAuthTokens): Promise<boolean> {
    try {
      await this.apiGet(tokens, '/limits');
      return true;
    } catch {
      return false;
    }
  }

  // ---------- SOQL Query Builder ----------

  private async soqlQuery<T>(
    tokens: OAuthTokens,
    soql: string,
  ): Promise<SalesforceQueryResult<T>> {
    const encodedQuery = encodeURIComponent(soql);
    return this.apiGet<SalesforceQueryResult<T>>(tokens, `/query?q=${encodedQuery}`);
  }

  // ---------- HTTP Helpers ----------

  private async apiGet<T>(tokens: OAuthTokens, path: string): Promise<T> {
    return this.apiRequest<T>(tokens, 'GET', path);
  }

  private async apiPost<T>(
    tokens: OAuthTokens,
    path: string,
    body: unknown,
  ): Promise<T> {
    return this.apiRequest<T>(tokens, 'POST', path, body);
  }

  private async apiPatch<T>(
    tokens: OAuthTokens,
    path: string,
    body: unknown,
  ): Promise<T> {
    return this.apiRequest<T>(tokens, 'PATCH', path, body);
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
    // Rate limiting
    await this.checkRateLimit();

    const baseUrl = `${tokens.instanceUrl}/services/data/${this.apiVersion}`;
    const url = `${baseUrl}${path}`;

    const headers: Record<string, string> = {
      Authorization: `Bearer ${tokens.accessToken}`,
      'Content-Type': 'application/json',
    };

    const options: RequestInit = { method, headers };
    if (body) {
      options.body = JSON.stringify(body);
    }

    const response = await fetch(url, options);

    // Update rate limit state from headers
    const remaining = response.headers.get('Sforce-Limit-Info');
    if (remaining) {
      const match = remaining.match(/api-usage=(\d+)\/(\d+)/);
      if (match) {
        this.rateLimitState.remaining = parseInt(match[2]) - parseInt(match[1]);
      }
    }

    // Handle 204 No Content (e.g., successful PATCH)
    if (response.status === 204) {
      return undefined as unknown as T;
    }

    // Handle rate limiting (429)
    if (response.status === 429 && retryCount < 3) {
      const retryAfter = parseInt(response.headers.get('Retry-After') || '5');
      this.logger.warn(`Salesforce rate limited, retrying in ${retryAfter}s`);
      await new Promise((resolve) => setTimeout(resolve, retryAfter * 1000));
      return this.apiRequest<T>(tokens, method, path, body, retryCount + 1);
    }

    if (!response.ok) {
      const errorBody = await response.text();
      this.logger.error(
        `Salesforce API error: ${method} ${path} - ${response.status} ${errorBody}`,
      );
      throw new Error(`Salesforce API error: ${response.status} ${response.statusText}`);
    }

    const text = await response.text();
    return text ? JSON.parse(text) : (undefined as unknown as T);
  }

  private async checkRateLimit(): Promise<void> {
    if (this.rateLimitState.remaining <= 5 && Date.now() < this.rateLimitState.resetAt) {
      const waitTime = this.rateLimitState.resetAt - Date.now();
      this.logger.warn(`Salesforce rate limit approaching, waiting ${waitTime}ms`);
      await new Promise((resolve) => setTimeout(resolve, Math.min(waitTime, 10000)));
    }
  }

  // ---------- Field Mapping ----------

  private mapToSalesforceContact(contact: Partial<CRMContact>): Record<string, unknown> {
    const sf: Record<string, unknown> = {};
    if (contact.firstName !== undefined) sf.FirstName = contact.firstName;
    if (contact.lastName !== undefined) sf.LastName = contact.lastName;
    if (contact.email !== undefined) sf.Email = contact.email;
    if (contact.phone !== undefined) sf.Phone = contact.phone;
    if (contact.title !== undefined) sf.Title = contact.title;
    if (contact.source !== undefined) sf.LeadSource = contact.source;
    if (contact.ownerId !== undefined) sf.OwnerId = contact.ownerId;

    // Merge custom fields
    if (contact.customFields) {
      Object.assign(sf, contact.customFields);
    }

    return sf;
  }
}
