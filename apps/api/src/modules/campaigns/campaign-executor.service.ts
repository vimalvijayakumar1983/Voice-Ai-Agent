import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { PrismaService } from '../../common/services/prisma.service';
import { RedisService } from '../../common/services/redis.service';
import { QueueService } from '../../common/services/queue.service';
import { CampaignStatus } from './dto/campaign.dto';

export enum PacingMode {
  PREDICTIVE = 'predictive',
  PROGRESSIVE = 'progressive',
  PREVIEW = 'preview',
}

export interface CampaignExecutionConfig {
  campaignId: string;
  tenantId: string;
  agentId: string;
  maxConcurrentCalls: number;
  callsPerHour: number;
  pacingMode: PacingMode;
  maxRetries: number;
  retryDelayMinutes: number;
  callingHours: {
    startHour: number;
    endHour: number;
    timezone: string;
    daysOfWeek: number[];
  };
}

export interface CampaignMetrics {
  totalContacts: number;
  contactsProcessed: number;
  contactsRemaining: number;
  activeCalls: number;
  completedCalls: number;
  failedCalls: number;
  busyCalls: number;
  noAnswerCalls: number;
  connectedCalls: number;
  totalRetries: number;
  successRate: number;
  callsPerMinute: number;
  averageDurationSeconds: number;
  startedAt: string;
  lastActivityAt: string;
}

interface ContactQueueItem {
  contactId: string;
  phone: string;
  firstName?: string;
  lastName?: string;
  timezone?: string;
  retryCount: number;
  lastAttemptAt?: string;
  metadata?: Record<string, unknown>;
}

@Injectable()
export class CampaignExecutorService {
  private readonly logger = new Logger(CampaignExecutorService.name);
  private readonly activeCampaigns = new Map<string, {
    config: CampaignExecutionConfig;
    isRunning: boolean;
    activeCallIds: Set<string>;
    intervalHandle: ReturnType<typeof setInterval> | null;
  }>();

  constructor(
    private readonly prisma: PrismaService,
    private readonly redisService: RedisService,
    private readonly queueService: QueueService,
    private readonly configService: ConfigService,
  ) {}

  /**
   * Starts campaign execution. Loads contacts, initializes the pacing engine,
   * and begins processing the contact queue.
   */
  async startCampaign(campaignId: string, tenantId: string): Promise<void> {
    this.logger.log(`Starting campaign execution: ${campaignId}`);

    const campaign = await this.prisma.campaign.findFirst({
      where: { id: campaignId, tenantId },
      include: { agent: true },
    });

    if (!campaign) {
      throw new Error(`Campaign ${campaignId} not found`);
    }

    const config: CampaignExecutionConfig = {
      campaignId,
      tenantId,
      agentId: campaign.agentId,
      maxConcurrentCalls: campaign.maxConcurrentCalls || 5,
      callsPerHour: campaign.callsPerHour || 100,
      pacingMode: PacingMode.PROGRESSIVE,
      maxRetries: campaign.maxRetries || 2,
      retryDelayMinutes: campaign.retryDelayMinutes || 60,
      callingHours: (campaign.callingHours as any) || {
        startHour: 9,
        endHour: 17,
        timezone: 'America/New_York',
        daysOfWeek: [1, 2, 3, 4, 5],
      },
    };

    // Load contacts into the queue
    await this.loadContactQueue(campaignId, tenantId, campaign.contactListIds as string[]);

    // Initialize metrics in Redis
    const totalContacts = await this.getQueueLength(campaignId);
    const initialMetrics: CampaignMetrics = {
      totalContacts,
      contactsProcessed: 0,
      contactsRemaining: totalContacts,
      activeCalls: 0,
      completedCalls: 0,
      failedCalls: 0,
      busyCalls: 0,
      noAnswerCalls: 0,
      connectedCalls: 0,
      totalRetries: 0,
      successRate: 0,
      callsPerMinute: 0,
      averageDurationSeconds: 0,
      startedAt: new Date().toISOString(),
      lastActivityAt: new Date().toISOString(),
    };
    await this.redisService.setJson(`campaign:metrics:${campaignId}`, initialMetrics, 86400);

    // Register active campaign
    this.activeCampaigns.set(campaignId, {
      config,
      isRunning: true,
      activeCallIds: new Set(),
      intervalHandle: null,
    });

    // Start the pacing engine
    this.startPacingEngine(campaignId);

    this.logger.log(`Campaign ${campaignId} started with ${totalContacts} contacts, max concurrent: ${config.maxConcurrentCalls}`);
  }

  /**
   * Pauses a running campaign. Active calls continue but no new calls are initiated.
   */
  async pauseCampaign(campaignId: string): Promise<void> {
    const campaignState = this.activeCampaigns.get(campaignId);
    if (!campaignState) {
      this.logger.warn(`Campaign ${campaignId} not found in active campaigns`);
      return;
    }

    campaignState.isRunning = false;

    if (campaignState.intervalHandle) {
      clearInterval(campaignState.intervalHandle);
      campaignState.intervalHandle = null;
    }

    this.logger.log(`Campaign ${campaignId} paused. Active calls: ${campaignState.activeCallIds.size}`);
  }

  /**
   * Resumes a paused campaign.
   */
  async resumeCampaign(campaignId: string, tenantId: string): Promise<void> {
    let campaignState = this.activeCampaigns.get(campaignId);

    if (!campaignState) {
      // Re-initialize from database
      await this.startCampaign(campaignId, tenantId);
      return;
    }

    campaignState.isRunning = true;
    this.startPacingEngine(campaignId);

    this.logger.log(`Campaign ${campaignId} resumed`);
  }

  /**
   * Handles the completion of a call within a campaign context.
   * Updates metrics, handles retries, and checks for campaign completion.
   */
  async handleCallCompleted(
    campaignId: string,
    callId: string,
    outcome: {
      status: string;
      durationSeconds: number;
      disposition?: string;
      contactId: string;
    },
  ): Promise<void> {
    const campaignState = this.activeCampaigns.get(campaignId);
    if (campaignState) {
      campaignState.activeCallIds.delete(callId);
    }

    // Update metrics
    const metrics = await this.getMetrics(campaignId);
    if (!metrics) return;

    metrics.activeCalls = campaignState?.activeCallIds.size ?? 0;
    metrics.contactsProcessed++;
    metrics.contactsRemaining = Math.max(0, metrics.contactsRemaining - 1);
    metrics.lastActivityAt = new Date().toISOString();

    switch (outcome.status) {
      case 'COMPLETED':
        metrics.completedCalls++;
        metrics.connectedCalls++;
        break;
      case 'FAILED':
        metrics.failedCalls++;
        break;
      case 'BUSY':
        metrics.busyCalls++;
        break;
      case 'NO_ANSWER':
        metrics.noAnswerCalls++;
        break;
      default:
        metrics.completedCalls++;
    }

    // Recalculate rates
    const totalProcessed = metrics.completedCalls + metrics.failedCalls + metrics.busyCalls + metrics.noAnswerCalls;
    metrics.successRate = totalProcessed > 0
      ? (metrics.connectedCalls / totalProcessed) * 100
      : 0;

    const elapsedMinutes = (Date.now() - new Date(metrics.startedAt).getTime()) / 60000;
    metrics.callsPerMinute = elapsedMinutes > 0 ? totalProcessed / elapsedMinutes : 0;

    if (outcome.durationSeconds > 0 && metrics.connectedCalls > 0) {
      const currentTotal = metrics.averageDurationSeconds * (metrics.connectedCalls - 1);
      metrics.averageDurationSeconds = (currentTotal + outcome.durationSeconds) / metrics.connectedCalls;
    }

    await this.redisService.setJson(`campaign:metrics:${campaignId}`, metrics, 86400);

    // Handle retries for failed/busy/no-answer calls
    if (['FAILED', 'BUSY', 'NO_ANSWER'].includes(outcome.status)) {
      await this.handleRetry(campaignId, outcome.contactId, outcome.status);
    }

    // Check if campaign is complete
    const remainingInQueue = await this.getQueueLength(campaignId);
    if (remainingInQueue === 0 && metrics.activeCalls === 0) {
      await this.completeCampaign(campaignId);
    }
  }

  /**
   * Gets real-time metrics for a campaign.
   */
  async getMetrics(campaignId: string): Promise<CampaignMetrics | null> {
    return this.redisService.getJson<CampaignMetrics>(`campaign:metrics:${campaignId}`);
  }

  /**
   * Checks if a campaign is currently being executed.
   */
  isExecuting(campaignId: string): boolean {
    return this.activeCampaigns.get(campaignId)?.isRunning ?? false;
  }

  /**
   * Gets the count of active calls for a campaign.
   */
  getActiveCalls(campaignId: string): number {
    return this.activeCampaigns.get(campaignId)?.activeCallIds.size ?? 0;
  }

  // ─── PRIVATE METHODS ──────────────────────────────────────────────────────────

  /**
   * Loads contacts from contact lists into a Redis-backed queue.
   */
  private async loadContactQueue(
    campaignId: string,
    tenantId: string,
    contactListIds: string[],
  ): Promise<void> {
    this.logger.log(`Loading contacts for campaign ${campaignId}`);

    // Fetch contacts from the database
    const contacts = await this.prisma.contact.findMany({
      where: {
        tenantId,
        // Filter by contact list IDs if provided
        ...(contactListIds.length > 0 ? { listId: { in: contactListIds } } : {}),
        doNotContact: false,
        phone: { not: null },
      },
      select: {
        id: true,
        phone: true,
        firstName: true,
        lastName: true,
        timezone: true,
        metadata: true,
      },
      orderBy: { createdAt: 'asc' },
    });

    // Filter out contacts that have already been successfully called in this campaign
    const existingCalls = await this.prisma.call.findMany({
      where: {
        campaignId,
        tenantId,
        status: 'COMPLETED',
      },
      select: { contactId: true },
    });

    const completedContactIds = new Set(existingCalls.map((c) => c.contactId).filter(Boolean));

    const queueItems: ContactQueueItem[] = contacts
      .filter((c) => !completedContactIds.has(c.id))
      .map((contact) => ({
        contactId: contact.id,
        phone: contact.phone!,
        firstName: contact.firstName || undefined,
        lastName: contact.lastName || undefined,
        timezone: contact.timezone || undefined,
        retryCount: 0,
        metadata: (contact.metadata as Record<string, unknown>) || {},
      }));

    // Push contacts to Redis list (FIFO queue)
    const queueKey = `campaign:queue:${campaignId}`;
    const redisClient = this.redisService.getClient();

    if (queueItems.length > 0) {
      const pipeline = redisClient.pipeline();
      // Clear existing queue
      pipeline.del(queueKey);
      // Add all contacts
      for (const item of queueItems) {
        pipeline.rpush(queueKey, JSON.stringify(item));
      }
      pipeline.expire(queueKey, 86400); // 24 hour TTL
      await pipeline.exec();
    }

    this.logger.log(`Loaded ${queueItems.length} contacts into queue for campaign ${campaignId}`);
  }

  /**
   * Starts the pacing engine that controls call initiation rate.
   */
  private startPacingEngine(campaignId: string): void {
    const campaignState = this.activeCampaigns.get(campaignId);
    if (!campaignState) return;

    const { config } = campaignState;
    const intervalMs = Math.max(1000, Math.floor(3600000 / config.callsPerHour));

    this.logger.log(`Pacing engine started for ${campaignId}: interval=${intervalMs}ms`);

    const processNext = async () => {
      if (!campaignState.isRunning) return;

      try {
        // Check calling window
        if (!this.isWithinCallingHours(config.callingHours)) {
          this.logger.debug(`Campaign ${campaignId} outside calling hours`);
          return;
        }

        // Check concurrent call limit
        if (campaignState.activeCallIds.size >= config.maxConcurrentCalls) {
          this.logger.debug(`Campaign ${campaignId} at max concurrent calls`);
          return;
        }

        // Dequeue next contact
        const contact = await this.dequeueContact(campaignId);
        if (!contact) {
          // Queue is empty, check if any calls are still active
          if (campaignState.activeCallIds.size === 0) {
            await this.completeCampaign(campaignId);
          }
          return;
        }

        // Validate contact before calling
        if (!this.isContactCallable(contact, config.callingHours)) {
          this.logger.debug(`Contact ${contact.contactId} not callable, re-queuing`);
          await this.requeueContact(campaignId, contact);
          return;
        }

        // Initiate the call via the job queue
        const jobId = `${campaignId}-${contact.contactId}-${Date.now()}`;
        await this.queueService.addJob('campaign-execution', 'process-contact', {
          tenantId: config.tenantId,
          campaignId,
          contactId: contact.contactId,
          phone: contact.phone,
          agentId: config.agentId,
          retryCount: contact.retryCount,
        }, {
          jobId,
          priority: contact.retryCount > 0 ? 5 : 1, // Lower priority for retries
        });

        campaignState.activeCallIds.add(jobId);

        this.logger.log(
          `Campaign ${campaignId}: initiating call to ${contact.phone} (contact: ${contact.contactId}, retry: ${contact.retryCount})`,
        );
      } catch (error) {
        this.logger.error(`Pacing engine error for campaign ${campaignId}: ${(error as Error).message}`);
      }
    };

    // Run immediately and then at interval
    processNext();
    campaignState.intervalHandle = setInterval(processNext, intervalMs);
  }

  /**
   * Dequeues the next contact from the Redis queue.
   */
  private async dequeueContact(campaignId: string): Promise<ContactQueueItem | null> {
    const queueKey = `campaign:queue:${campaignId}`;
    const raw = await this.redisService.getClient().lpop(queueKey);
    if (!raw) return null;

    try {
      return JSON.parse(raw) as ContactQueueItem;
    } catch {
      return null;
    }
  }

  /**
   * Re-queues a contact (pushes to the back of the queue).
   */
  private async requeueContact(campaignId: string, contact: ContactQueueItem): Promise<void> {
    const queueKey = `campaign:queue:${campaignId}`;
    await this.redisService.getClient().rpush(queueKey, JSON.stringify(contact));
  }

  /**
   * Gets the current queue length.
   */
  private async getQueueLength(campaignId: string): Promise<number> {
    const queueKey = `campaign:queue:${campaignId}`;
    return this.redisService.getClient().llen(queueKey);
  }

  /**
   * Handles retry logic for failed calls with exponential backoff.
   */
  private async handleRetry(
    campaignId: string,
    contactId: string,
    status: string,
  ): Promise<void> {
    const campaignState = this.activeCampaigns.get(campaignId);
    if (!campaignState) return;

    const { config } = campaignState;

    // Get the contact's retry count from Redis
    const retryKey = `campaign:retry:${campaignId}:${contactId}`;
    const currentRetries = parseInt(await this.redisService.get(retryKey) || '0', 10);

    if (currentRetries >= config.maxRetries) {
      this.logger.log(
        `Contact ${contactId} reached max retries (${config.maxRetries}) in campaign ${campaignId}`,
      );
      return;
    }

    // Calculate exponential backoff delay
    const baseDelayMs = config.retryDelayMinutes * 60 * 1000;
    const delayMs = baseDelayMs * Math.pow(2, currentRetries);

    // Increment retry count
    await this.redisService.set(retryKey, String(currentRetries + 1), 86400);

    // Schedule retry via job queue with delay
    await this.queueService.addJob('campaign-execution', 'retry-contact', {
      tenantId: config.tenantId,
      campaignId,
      contactId,
      agentId: config.agentId,
      retryCount: currentRetries + 1,
      previousStatus: status,
    }, {
      delay: delayMs,
      jobId: `retry-${campaignId}-${contactId}-${currentRetries + 1}`,
    });

    // Update metrics
    const metrics = await this.getMetrics(campaignId);
    if (metrics) {
      metrics.totalRetries++;
      await this.redisService.setJson(`campaign:metrics:${campaignId}`, metrics, 86400);
    }

    this.logger.log(
      `Scheduled retry ${currentRetries + 1}/${config.maxRetries} for contact ${contactId} ` +
      `in campaign ${campaignId}, delay: ${Math.round(delayMs / 60000)}m`,
    );
  }

  /**
   * Marks a campaign as completed.
   */
  private async completeCampaign(campaignId: string): Promise<void> {
    const campaignState = this.activeCampaigns.get(campaignId);
    if (campaignState) {
      campaignState.isRunning = false;
      if (campaignState.intervalHandle) {
        clearInterval(campaignState.intervalHandle);
      }
      this.activeCampaigns.delete(campaignId);
    }

    await this.prisma.campaign.update({
      where: { id: campaignId },
      data: {
        status: CampaignStatus.COMPLETED,
        completedAt: new Date(),
      },
    });

    this.logger.log(`Campaign ${campaignId} completed`);
  }

  /**
   * Checks if the current time is within the configured calling hours.
   */
  private isWithinCallingHours(callingHours: CampaignExecutionConfig['callingHours']): boolean {
    const now = new Date();

    // Convert to the campaign's timezone
    const formatter = new Intl.DateTimeFormat('en-US', {
      timeZone: callingHours.timezone,
      hour: 'numeric',
      hour12: false,
      weekday: 'short',
    });

    const parts = formatter.formatToParts(now);
    const hourPart = parts.find((p) => p.type === 'hour');
    const dayPart = parts.find((p) => p.type === 'weekday');

    const currentHour = parseInt(hourPart?.value || '0', 10);

    // Map day names to numbers (0=Sunday, 1=Monday, etc.)
    const dayMap: Record<string, number> = {
      Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6,
    };
    const currentDay = dayMap[dayPart?.value || ''] ?? now.getDay();

    // Check day of week
    if (!callingHours.daysOfWeek.includes(currentDay)) {
      return false;
    }

    // Check hour range
    return currentHour >= callingHours.startHour && currentHour < callingHours.endHour;
  }

  /**
   * Checks if a contact is callable based on their timezone and DNC status.
   */
  private isContactCallable(
    contact: ContactQueueItem,
    callingHours: CampaignExecutionConfig['callingHours'],
  ): boolean {
    // If the contact has a timezone, check their local calling hours
    if (contact.timezone) {
      try {
        const contactCallingHours = { ...callingHours, timezone: contact.timezone };
        return this.isWithinCallingHours(contactCallingHours);
      } catch {
        // If timezone is invalid, fall through to campaign timezone check
      }
    }

    return true; // Already checked campaign-level calling hours in the pacing engine
  }
}
