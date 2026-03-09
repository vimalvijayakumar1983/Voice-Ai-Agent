import { Logger } from '@nestjs/common';
import { Worker, Job } from 'bullmq';
import { ConfigService } from '@nestjs/config';
import { CRMSyncService } from '../crm/crm-sync.service';
import { RedisService } from '../../../common/services/redis.service';
import { CRMProvider, SyncOptions } from '../crm/crm.interface';

// ---------- Job Types ----------

interface FullSyncJobData {
  tenantId: string;
  integrationId: string;
  provider: CRMProvider;
  options: SyncOptions;
}

interface IncrementalSyncJobData {
  tenantId: string;
  integrationId: string;
  provider: CRMProvider;
  options: SyncOptions;
}

interface SyncContactJobData {
  tenantId: string;
  contactId: string;
  direction: 'inbound' | 'outbound';
}

interface LogActivityJobData {
  tenantId: string;
  contactExternalId: string;
  callId: string;
  subject: string;
  description: string;
  duration: number;
  outcome: string;
  direction: 'inbound' | 'outbound';
  recordingUrl?: string;
}

interface ProcessWebhookJobData {
  tenantId: string;
  provider: CRMProvider;
  payload: Record<string, unknown>;
}

type CRMSyncJobData =
  | FullSyncJobData
  | IncrementalSyncJobData
  | SyncContactJobData
  | LogActivityJobData
  | ProcessWebhookJobData;

// ---------- Processor ----------

export class CRMSyncProcessor {
  private readonly logger = new Logger(CRMSyncProcessor.name);
  private worker: Worker | null = null;

  constructor(
    private readonly crmSyncService: CRMSyncService,
    private readonly redisService: RedisService,
    private readonly configService: ConfigService,
  ) {}

  async start(): Promise<void> {
    const connection = {
      host: this.configService.get<string>('REDIS_HOST', 'localhost'),
      port: this.configService.get<number>('REDIS_PORT', 6379),
      password: this.configService.get<string>('REDIS_PASSWORD', '') || undefined,
    };

    this.worker = new Worker(
      'crm-sync',
      async (job: Job<CRMSyncJobData>) => {
        return this.processJob(job);
      },
      {
        connection,
        concurrency: this.configService.get<number>('CRM_SYNC_CONCURRENCY', 2),
        limiter: {
          max: this.configService.get<number>('CRM_SYNC_RATE_LIMIT', 5),
          duration: 60000,
        },
      },
    );

    this.worker.on('completed', (job: Job) => {
      this.logger.log(`CRM sync job ${job.name} (${job.id}) completed`);
    });

    this.worker.on('failed', (job: Job | undefined, error: Error) => {
      this.logger.error(
        `CRM sync job ${job?.name} (${job?.id}) failed: ${error.message}`,
        error.stack,
      );

      // Publish failure event
      if (job?.data?.tenantId) {
        this.publishEvent(job.data.tenantId, {
          type: `crm:${job.name}:failed`,
          error: error.message,
          jobId: job.id,
        }).catch(() => {});
      }
    });

    this.logger.log('CRM sync processor worker started');
  }

  async stop(): Promise<void> {
    if (this.worker) {
      await this.worker.close();
      this.worker = null;
      this.logger.log('CRM sync processor worker stopped');
    }
  }

  // ---------- Job Routing ----------

  private async processJob(job: Job<CRMSyncJobData>): Promise<unknown> {
    this.logger.log(
      `Processing CRM sync job ${job.name} (${job.id}) for tenant ${job.data.tenantId}`,
    );

    switch (job.name) {
      case 'full-sync':
        return this.handleFullSync(job as Job<FullSyncJobData>);

      case 'incremental-sync':
        return this.handleIncrementalSync(job as Job<IncrementalSyncJobData>);

      case 'sync-contact':
        return this.handleSyncContact(job as Job<SyncContactJobData>);

      case 'log-activity':
        return this.handleLogActivity(job as Job<LogActivityJobData>);

      case 'process-webhook':
        return this.handleProcessWebhook(job as Job<ProcessWebhookJobData>);

      default:
        this.logger.warn(`Unknown CRM sync job type: ${job.name}`);
        throw new Error(`Unknown CRM sync job type: ${job.name}`);
    }
  }

  // ---------- Job Handlers ----------

  private async handleFullSync(job: Job<FullSyncJobData>): Promise<unknown> {
    const { tenantId, integrationId, provider, options } = job.data;

    await this.publishEvent(tenantId, {
      type: 'crm:full-sync:started',
      provider,
    });

    const result = await this.crmSyncService.executSync(
      tenantId,
      integrationId,
      provider,
      { ...options, fullSync: true },
    );

    await this.publishEvent(tenantId, {
      type: 'crm:full-sync:completed',
      provider,
      result: {
        created: result.created,
        updated: result.updated,
        failed: result.failed,
        durationMs: result.durationMs,
      },
    });

    return result;
  }

  private async handleIncrementalSync(
    job: Job<IncrementalSyncJobData>,
  ): Promise<unknown> {
    const { tenantId, integrationId, provider, options } = job.data;

    await this.publishEvent(tenantId, {
      type: 'crm:incremental-sync:started',
      provider,
    });

    const result = await this.crmSyncService.executSync(
      tenantId,
      integrationId,
      provider,
      { ...options, fullSync: false },
    );

    await this.publishEvent(tenantId, {
      type: 'crm:incremental-sync:completed',
      provider,
      result: {
        created: result.created,
        updated: result.updated,
        failed: result.failed,
        durationMs: result.durationMs,
      },
    });

    return result;
  }

  private async handleSyncContact(
    job: Job<SyncContactJobData>,
  ): Promise<unknown> {
    const { tenantId, contactId, direction } = job.data;

    this.logger.log(
      `Syncing contact ${contactId} ${direction} for tenant ${tenantId}`,
    );

    // For individual contact sync, trigger a mini-sync
    await this.crmSyncService.triggerSync(tenantId, {
      entityTypes: ['contact'],
      batchSize: 1,
    });

    return { contactId, direction, synced: true };
  }

  private async handleLogActivity(
    job: Job<LogActivityJobData>,
  ): Promise<unknown> {
    const {
      tenantId,
      contactExternalId,
      callId,
      subject,
      description,
      duration,
      outcome,
      direction,
      recordingUrl,
    } = job.data;

    await this.crmSyncService.logCallActivity(tenantId, {
      type: 'call',
      subject,
      description,
      contactId: contactExternalId,
      duration,
      outcome,
      callDirection: direction,
      recordingUrl,
      completedAt: new Date().toISOString(),
      customFields: { voiceAiCallId: callId },
    });

    await this.publishEvent(tenantId, {
      type: 'crm:activity-logged',
      callId,
      contactExternalId,
    });

    return { logged: true, callId };
  }

  private async handleProcessWebhook(
    job: Job<ProcessWebhookJobData>,
  ): Promise<unknown> {
    const { tenantId, provider, payload } = job.data;

    this.logger.log(
      `Processing ${provider} webhook for tenant ${tenantId}`,
    );

    // Determine what changed and trigger targeted sync
    // This is provider-specific webhook parsing
    if (provider === CRMProvider.SALESFORCE) {
      return this.processSalesforceWebhook(tenantId, payload);
    } else if (provider === CRMProvider.HUBSPOT) {
      return this.processHubSpotWebhook(tenantId, payload);
    }

    return { processed: true };
  }

  private async processSalesforceWebhook(
    tenantId: string,
    payload: Record<string, unknown>,
  ): Promise<unknown> {
    // Salesforce Outbound Message or Platform Event
    const events = (payload.notifications || payload.events || [payload]) as Array<Record<string, unknown>>;

    for (const event of events) {
      const objectType = event.sObjectType || event.sobjectType;
      const eventType = event.type || event.eventType;

      this.logger.log(
        `Salesforce webhook: ${objectType} ${eventType} for tenant ${tenantId}`,
      );

      // Trigger incremental sync for the affected entity type
      await this.crmSyncService.triggerSync(tenantId, {
        entityTypes: [String(objectType).toLowerCase()],
        batchSize: 50,
      });
    }

    return { processed: true, eventCount: events.length };
  }

  private async processHubSpotWebhook(
    tenantId: string,
    payload: Record<string, unknown>,
  ): Promise<unknown> {
    // HubSpot webhook payload is an array of subscription events
    const events = Array.isArray(payload) ? payload : [payload];

    for (const event of events) {
      const subscriptionType = event.subscriptionType as string;
      const objectType = event.objectType || (subscriptionType?.split('.')[0]);

      this.logger.log(
        `HubSpot webhook: ${subscriptionType} for tenant ${tenantId}`,
      );

      await this.crmSyncService.triggerSync(tenantId, {
        entityTypes: [String(objectType).toLowerCase()],
        batchSize: 50,
      });
    }

    return { processed: true, eventCount: events.length };
  }

  // ---------- Events ----------

  private async publishEvent(
    tenantId: string,
    event: Record<string, unknown>,
  ): Promise<void> {
    try {
      const client = this.redisService.getClient();
      await client.publish(
        'crm:events',
        JSON.stringify({ tenantId, ...event, timestamp: new Date().toISOString() }),
      );
    } catch (error) {
      this.logger.warn(`Failed to publish CRM event: ${(error as Error).message}`);
    }
  }
}
