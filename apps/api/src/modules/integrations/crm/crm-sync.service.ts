import { Injectable, Logger, NotFoundException, BadRequestException } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { PrismaService } from '../../../common/services/prisma.service';
import { RedisService } from '../../../common/services/redis.service';
import { QueueService } from '../../../common/services/queue.service';
import {
  ICRMProvider,
  CRMProvider,
  CRMContact,
  CRMActivity,
  OAuthTokens,
  SyncDirection,
  SyncStatus,
  SyncResult,
  SyncError,
  SyncOptions,
  ConflictResolution,
  FieldMapping,
  FieldMappingConfig,
  FieldTransformType,
} from './crm.interface';
import { SalesforceProvider } from './providers/salesforce.provider';
import { HubSpotProvider } from './providers/hubspot.provider';

// ---------- Types ----------

interface IntegrationRecord {
  id: string;
  tenantId: string;
  provider: string;
  status: string;
  config: Record<string, unknown>;
  credentials: Record<string, unknown>;
  lastSyncAt?: Date;
  metadata?: Record<string, unknown>;
}

// ---------- Service ----------

@Injectable()
export class CRMSyncService {
  private readonly logger = new Logger(CRMSyncService.name);
  private readonly providers = new Map<string, ICRMProvider>();

  constructor(
    private readonly prisma: PrismaService,
    private readonly redisService: RedisService,
    private readonly queueService: QueueService,
    private readonly configService: ConfigService,
    private readonly salesforceProvider: SalesforceProvider,
    private readonly hubspotProvider: HubSpotProvider,
  ) {
    this.providers.set(CRMProvider.SALESFORCE, salesforceProvider);
    this.providers.set(CRMProvider.HUBSPOT, hubspotProvider);
  }

  // ---------- OAuth Flow ----------

  async initiateOAuthFlow(
    tenantId: string,
    provider: CRMProvider,
  ): Promise<{ authorizationUrl: string; state: string }> {
    const crmProvider = this.getProvider(provider);

    // Generate state token for CSRF protection
    const state = Buffer.from(
      JSON.stringify({
        tenantId,
        provider,
        timestamp: Date.now(),
        nonce: Math.random().toString(36).substring(2),
      }),
    ).toString('base64url');

    // Store state in Redis for validation
    await this.redisService.set(`crm:oauth:state:${state}`, tenantId, 600);

    const authorizationUrl = crmProvider.getAuthorizationUrl(state);

    this.logger.log(`Initiated ${provider} OAuth flow for tenant ${tenantId}`);

    return { authorizationUrl, state };
  }

  async handleOAuthCallback(
    code: string,
    state: string,
  ): Promise<{ integrationId: string; provider: string }> {
    // Validate state
    const tenantId = await this.redisService.get(`crm:oauth:state:${state}`);
    if (!tenantId) {
      throw new BadRequestException('Invalid or expired OAuth state');
    }

    // Decode state to get provider
    const stateData = JSON.parse(Buffer.from(state, 'base64url').toString());
    const provider = stateData.provider as CRMProvider;
    const crmProvider = this.getProvider(provider);

    // Exchange code for tokens
    const tokens = await crmProvider.exchangeCodeForTokens(code);

    // Test the connection
    const isValid = await crmProvider.testConnection(tokens);
    if (!isValid) {
      throw new BadRequestException('Failed to validate CRM connection');
    }

    // Store integration record
    const integration = await this.prisma.integration.upsert({
      where: {
        tenantId_provider: {
          tenantId,
          provider,
        },
      } as any,
      update: {
        status: 'ACTIVE',
        credentials: {
          accessToken: tokens.accessToken,
          refreshToken: tokens.refreshToken,
          expiresAt: tokens.expiresAt?.toISOString(),
          instanceUrl: tokens.instanceUrl,
        } as any,
        metadata: {
          connectedAt: new Date().toISOString(),
          tokenType: tokens.tokenType,
        } as any,
      },
      create: {
        tenantId,
        provider,
        type: 'crm',
        name: `${provider} CRM`,
        status: 'ACTIVE',
        config: {
          syncDirection: SyncDirection.BIDIRECTIONAL,
          conflictResolution: ConflictResolution.NEWEST_WINS,
          syncInterval: 300, // 5 minutes
        } as any,
        credentials: {
          accessToken: tokens.accessToken,
          refreshToken: tokens.refreshToken,
          expiresAt: tokens.expiresAt?.toISOString(),
          instanceUrl: tokens.instanceUrl,
        } as any,
        metadata: {
          connectedAt: new Date().toISOString(),
          tokenType: tokens.tokenType,
        } as any,
      },
    });

    // Clean up state
    await this.redisService.del(`crm:oauth:state:${state}`);

    this.logger.log(`${provider} CRM connected for tenant ${tenantId}`);

    return { integrationId: integration.id, provider };
  }

  // ---------- Sync Operations ----------

  async triggerSync(
    tenantId: string,
    options?: SyncOptions,
  ): Promise<{ jobId: string; status: string }> {
    const integration = await this.getActiveIntegration(tenantId);

    // Check if sync is already in progress
    const currentStatus = await this.getSyncStatus(tenantId);
    if (currentStatus.status === SyncStatus.SYNCING) {
      throw new BadRequestException('A sync is already in progress');
    }

    // Queue sync job
    const jobName = options?.fullSync ? 'full-sync' : 'incremental-sync';
    const job = await this.queueService.addJob('crm-sync', jobName, {
      tenantId,
      integrationId: integration.id,
      provider: integration.provider,
      options: options || {},
    });

    // Update sync status
    await this.setSyncStatus(tenantId, SyncStatus.SYNCING, 'Sync initiated');

    this.logger.log(
      `${jobName} queued for tenant ${tenantId} (job ${job.id})`,
    );

    return { jobId: job.id as string, status: 'queued' };
  }

  async executSync(
    tenantId: string,
    integrationId: string,
    provider: CRMProvider,
    options: SyncOptions,
  ): Promise<SyncResult> {
    const startTime = Date.now();
    const crmProvider = this.getProvider(provider);
    const tokens = await this.getTokens(tenantId, integrationId);
    const integration = await this.prisma.integration.findUnique({
      where: { id: integrationId },
    });

    const syncConfig = (integration?.config as Record<string, unknown>) || {};
    const syncDirection = (syncConfig.syncDirection as SyncDirection) || SyncDirection.BIDIRECTIONAL;
    const conflictResolution = (syncConfig.conflictResolution as ConflictResolution) || ConflictResolution.NEWEST_WINS;

    const since = options.fullSync ? undefined : (integration?.lastSyncAt || undefined) as Date | undefined;
    const errors: SyncError[] = [];
    let created = 0;
    let updated = 0;
    let deleted = 0;
    let failed = 0;

    try {
      // Sync contacts from CRM to Voice AI (inbound)
      if (syncDirection === SyncDirection.INBOUND || syncDirection === SyncDirection.BIDIRECTIONAL) {
        const inboundResult = await this.syncContactsInbound(
          tenantId,
          crmProvider,
          tokens,
          since,
          conflictResolution,
          options.batchSize,
        );
        created += inboundResult.created;
        updated += inboundResult.updated;
        errors.push(...inboundResult.errors);
        failed += inboundResult.errors.length;
      }

      // Sync contacts from Voice AI to CRM (outbound)
      if (syncDirection === SyncDirection.OUTBOUND || syncDirection === SyncDirection.BIDIRECTIONAL) {
        const outboundResult = await this.syncContactsOutbound(
          tenantId,
          crmProvider,
          tokens,
          since,
          options.batchSize,
        );
        created += outboundResult.created;
        updated += outboundResult.updated;
        errors.push(...outboundResult.errors);
        failed += outboundResult.errors.length;
      }

      // Update last sync timestamp
      await this.prisma.integration.update({
        where: { id: integrationId },
        data: { lastSyncAt: new Date() },
      });

      const status = errors.length > 0 ? SyncStatus.PARTIAL : SyncStatus.SUCCESS;
      await this.setSyncStatus(tenantId, status, `Sync completed: ${created} created, ${updated} updated`);

    } catch (error) {
      await this.setSyncStatus(tenantId, SyncStatus.FAILED, (error as Error).message);
      throw error;
    }

    const result: SyncResult = {
      provider,
      entityType: 'contact',
      direction: syncDirection,
      created,
      updated,
      deleted,
      failed,
      errors,
      startedAt: new Date(startTime).toISOString(),
      completedAt: new Date().toISOString(),
      durationMs: Date.now() - startTime,
    };

    // Store sync history
    await this.storeSyncHistory(tenantId, integrationId, result);

    this.logger.log(
      `CRM sync completed for tenant ${tenantId}: ${created} created, ${updated} updated, ${failed} failed in ${result.durationMs}ms`,
    );

    return result;
  }

  // ---------- Inbound Sync (CRM -> Voice AI) ----------

  private async syncContactsInbound(
    tenantId: string,
    provider: ICRMProvider,
    tokens: OAuthTokens,
    since?: Date,
    conflictResolution: ConflictResolution = ConflictResolution.NEWEST_WINS,
    batchSize = 200,
  ): Promise<{ created: number; updated: number; errors: SyncError[] }> {
    let created = 0;
    let updated = 0;
    const errors: SyncError[] = [];
    let hasMore = true;
    let offset = 0;

    while (hasMore) {
      const { contacts, hasMore: more } = await provider.getContacts(
        tokens,
        since,
        batchSize,
        offset,
      );

      hasMore = more;
      offset += contacts.length;

      for (const crmContact of contacts) {
        try {
          // Find existing contact by externalId or email
          const existing = await this.prisma.contact.findFirst({
            where: {
              tenantId,
              OR: [
                { externalId: crmContact.externalId },
                ...(crmContact.email ? [{ email: crmContact.email }] : []),
              ],
            },
          });

          if (existing) {
            // Resolve conflict
            const shouldUpdate = this.shouldUpdateLocal(
              existing,
              crmContact,
              conflictResolution,
            );

            if (shouldUpdate) {
              await this.prisma.contact.update({
                where: { id: existing.id },
                data: {
                  firstName: crmContact.firstName,
                  lastName: crmContact.lastName,
                  email: crmContact.email,
                  phone: crmContact.phone,
                  company: crmContact.company,
                  externalId: crmContact.externalId,
                  metadata: {
                    ...(existing.metadata as Record<string, unknown> || {}),
                    crmProvider: provider.providerName,
                    lastSyncedAt: new Date().toISOString(),
                  } as any,
                },
              });
              updated++;
            }
          } else {
            await this.prisma.contact.create({
              data: {
                tenantId,
                firstName: crmContact.firstName,
                lastName: crmContact.lastName,
                email: crmContact.email || '',
                phone: crmContact.phone,
                company: crmContact.company,
                externalId: crmContact.externalId,
                source: `crm:${provider.providerName}`,
                metadata: {
                  crmProvider: provider.providerName,
                  lastSyncedAt: new Date().toISOString(),
                } as any,
              },
            });
            created++;
          }
        } catch (error) {
          errors.push({
            externalId: crmContact.externalId,
            operation: existing ? 'update' : 'create',
            message: (error as Error).message,
          });
        }
      }
    }

    return { created, updated, errors };
  }

  // ---------- Outbound Sync (Voice AI -> CRM) ----------

  private async syncContactsOutbound(
    tenantId: string,
    provider: ICRMProvider,
    tokens: OAuthTokens,
    since?: Date,
    batchSize = 200,
  ): Promise<{ created: number; updated: number; errors: SyncError[] }> {
    let created = 0;
    let updated = 0;
    const errors: SyncError[] = [];

    // Find contacts that need syncing
    const whereClause: Record<string, unknown> = { tenantId };
    if (since) {
      whereClause.updatedAt = { gte: since };
    }

    const contacts = await this.prisma.contact.findMany({
      where: whereClause as any,
      orderBy: { updatedAt: 'desc' },
      take: batchSize,
    });

    for (const contact of contacts) {
      try {
        const crmContact: CRMContact = {
          firstName: contact.firstName,
          lastName: contact.lastName,
          email: contact.email || undefined,
          phone: contact.phone || undefined,
          company: contact.company || undefined,
        };

        if (contact.externalId) {
          // Update existing CRM contact
          await provider.updateContact(tokens, contact.externalId, crmContact);
          updated++;
        } else {
          // Create new CRM contact
          const result = await provider.createContact(tokens, crmContact);

          // Store external ID
          await this.prisma.contact.update({
            where: { id: contact.id },
            data: {
              externalId: result.externalId,
              metadata: {
                ...(contact.metadata as Record<string, unknown> || {}),
                crmProvider: provider.providerName,
                lastSyncedAt: new Date().toISOString(),
              } as any,
            },
          });
          created++;
        }
      } catch (error) {
        errors.push({
          entityId: contact.id,
          externalId: contact.externalId || undefined,
          operation: contact.externalId ? 'update' : 'create',
          message: (error as Error).message,
        });
      }
    }

    return { created, updated, errors };
  }

  // ---------- Activity Logging ----------

  async logCallActivity(
    tenantId: string,
    activity: CRMActivity,
  ): Promise<void> {
    const integration = await this.getActiveIntegration(tenantId);
    const provider = this.getProvider(integration.provider as CRMProvider);
    const tokens = await this.getTokens(tenantId, integration.id);

    await provider.logActivity(tokens, activity);

    this.logger.log(
      `Logged call activity for contact ${activity.contactId} in ${integration.provider}`,
    );
  }

  // ---------- Webhook Handler ----------

  async handleWebhook(
    tenantId: string,
    provider: CRMProvider,
    payload: Record<string, unknown>,
  ): Promise<void> {
    this.logger.log(`Received ${provider} webhook for tenant ${tenantId}`);

    // Queue the webhook processing
    await this.queueService.addJob('crm-sync', 'process-webhook', {
      tenantId,
      provider,
      payload,
    });
  }

  // ---------- Field Mappings ----------

  async getFieldMappings(tenantId: string): Promise<FieldMappingConfig[]> {
    const integration = await this.getActiveIntegration(tenantId);
    const config = integration.config as Record<string, unknown>;
    return (config.fieldMappings as FieldMappingConfig[]) || [];
  }

  async updateFieldMappings(
    tenantId: string,
    mappings: FieldMappingConfig[],
  ): Promise<FieldMappingConfig[]> {
    const integration = await this.getActiveIntegration(tenantId);
    const config = (integration.config as Record<string, unknown>) || {};

    await this.prisma.integration.update({
      where: { id: integration.id },
      data: {
        config: {
          ...config,
          fieldMappings: mappings,
        } as any,
      },
    });

    return mappings;
  }

  // ---------- Status ----------

  async getSyncStatus(tenantId: string): Promise<{
    status: SyncStatus;
    message: string;
    lastSyncAt?: string;
    provider?: string;
  }> {
    const statusData = await this.redisService.getJson<{
      status: SyncStatus;
      message: string;
      updatedAt: string;
    }>(`crm:sync:status:${tenantId}`);

    const integration = await this.prisma.integration.findFirst({
      where: { tenantId, type: 'crm', status: 'ACTIVE' },
    });

    return {
      status: statusData?.status || SyncStatus.IDLE,
      message: statusData?.message || 'No sync in progress',
      lastSyncAt: integration?.lastSyncAt?.toISOString(),
      provider: integration?.provider,
    };
  }

  async disconnect(tenantId: string): Promise<void> {
    const integration = await this.getActiveIntegration(tenantId);

    await this.prisma.integration.update({
      where: { id: integration.id },
      data: {
        status: 'DISCONNECTED',
        credentials: {} as any,
      },
    });

    // Clear sync status
    await this.redisService.del(`crm:sync:status:${tenantId}`);

    this.logger.log(`CRM disconnected for tenant ${tenantId}`);
  }

  // ---------- Transform Helpers ----------

  applyFieldTransform(value: unknown, transform: FieldMapping['transform']): unknown {
    if (!transform || !value) return value;

    const strValue = String(value);

    switch (transform.type) {
      case FieldTransformType.UPPERCASE:
        return strValue.toUpperCase();

      case FieldTransformType.LOWERCASE:
        return strValue.toLowerCase();

      case FieldTransformType.CAPITALIZE:
        return strValue.replace(/\b\w/g, (c) => c.toUpperCase());

      case FieldTransformType.FORMAT_PHONE:
        // Strip non-numeric and format as +1XXXXXXXXXX
        const digits = strValue.replace(/\D/g, '');
        if (digits.length === 10) return `+1${digits}`;
        if (digits.length === 11 && digits.startsWith('1')) return `+${digits}`;
        return `+${digits}`;

      case FieldTransformType.FORMAT_DATE:
        return new Date(strValue).toISOString();

      case FieldTransformType.MAP_VALUES:
        const mapping = (transform.config?.valueMap || {}) as Record<string, string>;
        return mapping[strValue] || value;

      default:
        return value;
    }
  }

  // ---------- Private Helpers ----------

  private getProvider(provider: CRMProvider): ICRMProvider {
    const p = this.providers.get(provider);
    if (!p) {
      throw new BadRequestException(`Unsupported CRM provider: ${provider}`);
    }
    return p;
  }

  private async getActiveIntegration(tenantId: string): Promise<IntegrationRecord> {
    const integration = await this.prisma.integration.findFirst({
      where: { tenantId, type: 'crm', status: 'ACTIVE' },
    });

    if (!integration) {
      throw new NotFoundException('No active CRM integration found');
    }

    return integration as unknown as IntegrationRecord;
  }

  private async getTokens(tenantId: string, integrationId: string): Promise<OAuthTokens> {
    const integration = await this.prisma.integration.findUnique({
      where: { id: integrationId },
    });

    if (!integration) {
      throw new NotFoundException('Integration not found');
    }

    const credentials = integration.credentials as Record<string, unknown>;
    const tokens: OAuthTokens = {
      accessToken: credentials.accessToken as string,
      refreshToken: credentials.refreshToken as string | undefined,
      tokenType: 'Bearer',
      instanceUrl: credentials.instanceUrl as string | undefined,
    };

    // Check if token is expired and refresh
    if (credentials.expiresAt) {
      const expiresAt = new Date(credentials.expiresAt as string);
      if (expiresAt <= new Date()) {
        const provider = this.getProvider(integration.provider as CRMProvider);
        if (tokens.refreshToken) {
          const newTokens = await provider.refreshAccessToken(tokens.refreshToken);

          // Update stored credentials
          await this.prisma.integration.update({
            where: { id: integrationId },
            data: {
              credentials: {
                accessToken: newTokens.accessToken,
                refreshToken: newTokens.refreshToken || tokens.refreshToken,
                expiresAt: newTokens.expiresAt?.toISOString(),
                instanceUrl: newTokens.instanceUrl || tokens.instanceUrl,
              } as any,
            },
          });

          return newTokens;
        }
      }
    }

    return tokens;
  }

  private shouldUpdateLocal(
    existing: Record<string, unknown>,
    incoming: CRMContact,
    conflictResolution: ConflictResolution,
  ): boolean {
    switch (conflictResolution) {
      case ConflictResolution.CRM_WINS:
        return true;
      case ConflictResolution.VOICE_AI_WINS:
        return false;
      case ConflictResolution.NEWEST_WINS:
        const localUpdated = existing.updatedAt ? new Date(existing.updatedAt as string) : new Date(0);
        const remoteUpdated = incoming.updatedAt ? new Date(incoming.updatedAt) : new Date(0);
        return remoteUpdated >= localUpdated;
      default:
        return true;
    }
  }

  private async setSyncStatus(
    tenantId: string,
    status: SyncStatus,
    message: string,
  ): Promise<void> {
    await this.redisService.setJson(
      `crm:sync:status:${tenantId}`,
      { status, message, updatedAt: new Date().toISOString() },
      86400,
    );
  }

  private async storeSyncHistory(
    tenantId: string,
    integrationId: string,
    result: SyncResult,
  ): Promise<void> {
    try {
      await this.prisma.auditLog.create({
        data: {
          tenantId,
          action: 'crm_sync',
          entityType: 'integration',
          entityId: integrationId,
          metadata: result as any,
        },
      });
    } catch (error) {
      this.logger.warn(`Failed to store sync history: ${(error as Error).message}`);
    }
  }
}
