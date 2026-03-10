import { Injectable, Logger, OnModuleInit } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { PrismaService } from '../../common/services/prisma.service';
import { RedisService } from '../../common/services/redis.service';
import { QueueService } from '../../common/services/queue.service';
import { CampaignExecutorService } from './campaign-executor.service';
import { CampaignStatus } from './dto/campaign.dto';

interface SchedulerConfig {
  checkIntervalMs: number;
  autoStartEnabled: boolean;
  autoPauseEnabled: boolean;
}

/**
 * Campaign scheduler that runs on a cron-like interval.
 * Checks for campaigns that should start, auto-pauses campaigns outside calling windows,
 * and manages rate limiting per tenant.
 */
@Injectable()
export class CampaignSchedulerService implements OnModuleInit {
  private readonly logger = new Logger(CampaignSchedulerService.name);
  private readonly config: SchedulerConfig;
  private schedulerInterval: ReturnType<typeof setInterval> | null = null;
  private isProcessing = false;

  constructor(
    private readonly prisma: PrismaService,
    private readonly redisService: RedisService,
    private readonly queueService: QueueService,
    private readonly configService: ConfigService,
    private readonly campaignExecutor: CampaignExecutorService,
  ) {
    this.config = {
      checkIntervalMs: this.configService.get<number>('CAMPAIGN_SCHEDULER_INTERVAL_MS', 30000),
      autoStartEnabled: this.configService.get<boolean>('CAMPAIGN_AUTO_START_ENABLED', true),
      autoPauseEnabled: this.configService.get<boolean>('CAMPAIGN_AUTO_PAUSE_ENABLED', true),
    };
  }

  async onModuleInit(): Promise<void> {
    this.startScheduler();
    this.logger.log(
      `Campaign scheduler initialized, check interval: ${this.config.checkIntervalMs}ms`,
    );
  }

  /**
   * Starts the scheduler loop that periodically checks for campaigns to manage.
   */
  startScheduler(): void {
    if (this.schedulerInterval) return;

    this.schedulerInterval = setInterval(async () => {
      if (this.isProcessing) return;

      this.isProcessing = true;
      try {
        await this.runSchedulerCycle();
      } catch (error) {
        this.logger.error(`Scheduler cycle error: ${(error as Error).message}`);
      } finally {
        this.isProcessing = false;
      }
    }, this.config.checkIntervalMs);

    this.logger.log('Campaign scheduler started');
  }

  /**
   * Stops the scheduler loop.
   */
  stopScheduler(): void {
    if (this.schedulerInterval) {
      clearInterval(this.schedulerInterval);
      this.schedulerInterval = null;
      this.logger.log('Campaign scheduler stopped');
    }
  }

  /**
   * Runs a single scheduler cycle. Can also be called manually.
   */
  async runSchedulerCycle(): Promise<void> {
    // Acquire a distributed lock to prevent multiple instances from running
    const lockKey = 'scheduler:campaign:lock';
    const lockAcquired = await this.acquireLock(lockKey, 60);
    if (!lockAcquired) {
      this.logger.debug('Scheduler lock held by another instance, skipping cycle');
      return;
    }

    try {
      await Promise.all([
        this.checkScheduledCampaigns(),
        this.checkCallingWindowEnforcement(),
        this.checkTenantRateLimits(),
      ]);
    } finally {
      await this.releaseLock(lockKey);
    }
  }

  // ─── SCHEDULED CAMPAIGN CHECKS ────────────────────────────────────────────────

  /**
   * Checks for campaigns with a scheduledStartAt time that has passed and starts them.
   */
  private async checkScheduledCampaigns(): Promise<void> {
    if (!this.config.autoStartEnabled) return;

    const now = new Date();

    const campaignsToStart = await this.prisma.campaign.findMany({
      where: {
        status: CampaignStatus.SCHEDULED,
        scheduledStartAt: { lte: now },
        // Don't start if end time has already passed
        OR: [
          { completedAt: null },
          { completedAt: { gt: now } },
        ],
      },
      select: {
        id: true,
        tenantId: true,
        name: true,
        scheduledStartAt: true,
      },
    });

    for (const campaign of campaignsToStart) {
      try {
        // Check tenant rate limit before starting
        const canStart = await this.checkTenantCanStartCampaign(campaign.tenantId);
        if (!canStart) {
          this.logger.warn(
            `Tenant ${campaign.tenantId} rate limited, deferring campaign ${campaign.id}`,
          );
          continue;
        }

        this.logger.log(
          `Auto-starting scheduled campaign: ${campaign.name} (${campaign.id}), ` +
          `scheduled for ${campaign.scheduledStartAt?.toISOString()}`,
        );

        // Update status to RUNNING
        await this.prisma.campaign.update({
          where: { id: campaign.id },
          data: {
            status: CampaignStatus.RUNNING,
            startedAt: now,
          },
        });

        // Queue the campaign execution job
        await this.queueService.addJob('campaign-execution', 'start-campaign', {
          tenantId: campaign.tenantId,
          campaignId: campaign.id,
        });
      } catch (error) {
        this.logger.error(
          `Failed to auto-start campaign ${campaign.id}: ${(error as Error).message}`,
        );
      }
    }

    if (campaignsToStart.length > 0) {
      this.logger.log(`Processed ${campaignsToStart.length} scheduled campaigns`);
    }
  }

  // ─── CALLING WINDOW ENFORCEMENT ───────────────────────────────────────────────

  /**
   * Auto-pauses running campaigns that are outside their calling window,
   * and auto-resumes paused campaigns that are back in their calling window.
   */
  private async checkCallingWindowEnforcement(): Promise<void> {
    if (!this.config.autoPauseEnabled) return;

    // Find running campaigns and check their calling hours
    const runningCampaigns = await this.prisma.campaign.findMany({
      where: { status: CampaignStatus.RUNNING },
      select: {
        id: true,
        tenantId: true,
        name: true,
        callingHours: true,
      },
    });

    for (const campaign of runningCampaigns) {
      const callingHours = campaign.callingHours as {
        startHour?: number;
        endHour?: number;
        timezone?: string;
        daysOfWeek?: number[];
      } | null;

      if (!callingHours?.startHour || !callingHours?.endHour) continue;

      const isWithinWindow = this.isWithinCallingWindow(
        callingHours.startHour,
        callingHours.endHour,
        callingHours.timezone || 'America/New_York',
        callingHours.daysOfWeek || [1, 2, 3, 4, 5],
      );

      if (!isWithinWindow) {
        this.logger.log(
          `Auto-pausing campaign ${campaign.name} (${campaign.id}) - outside calling window`,
        );

        await this.prisma.campaign.update({
          where: { id: campaign.id },
          data: { status: CampaignStatus.PAUSED },
        });

        // Mark as auto-paused so we can auto-resume later
        await this.redisService.set(`campaign:autopaused:${campaign.id}`, 'true', 86400);

        // Pause the executor
        await this.campaignExecutor.pauseCampaign(campaign.id);
      }
    }

    // Check auto-paused campaigns for auto-resume
    const pausedCampaigns = await this.prisma.campaign.findMany({
      where: { status: CampaignStatus.PAUSED },
      select: {
        id: true,
        tenantId: true,
        name: true,
        callingHours: true,
      },
    });

    for (const campaign of pausedCampaigns) {
      // Only auto-resume campaigns that were auto-paused
      const wasAutoPaused = await this.redisService.get(`campaign:autopaused:${campaign.id}`);
      if (!wasAutoPaused) continue;

      const callingHours = campaign.callingHours as {
        startHour?: number;
        endHour?: number;
        timezone?: string;
        daysOfWeek?: number[];
      } | null;

      if (!callingHours?.startHour || !callingHours?.endHour) continue;

      const isWithinWindow = this.isWithinCallingWindow(
        callingHours.startHour,
        callingHours.endHour,
        callingHours.timezone || 'America/New_York',
        callingHours.daysOfWeek || [1, 2, 3, 4, 5],
      );

      if (isWithinWindow) {
        this.logger.log(
          `Auto-resuming campaign ${campaign.name} (${campaign.id}) - back in calling window`,
        );

        await this.prisma.campaign.update({
          where: { id: campaign.id },
          data: { status: CampaignStatus.RUNNING },
        });

        await this.redisService.del(`campaign:autopaused:${campaign.id}`);

        // Resume execution
        await this.queueService.addJob('campaign-execution', 'resume-campaign', {
          tenantId: campaign.tenantId,
          campaignId: campaign.id,
        });
      }
    }
  }

  // ─── RATE LIMITING ────────────────────────────────────────────────────────────

  /**
   * Checks and enforces per-tenant rate limits for campaign execution.
   */
  private async checkTenantRateLimits(): Promise<void> {
    const runningCampaigns = await this.prisma.campaign.findMany({
      where: { status: CampaignStatus.RUNNING },
      select: { id: true, tenantId: true },
    });

    // Group by tenant
    const tenantCampaigns = new Map<string, string[]>();
    for (const campaign of runningCampaigns) {
      const existing = tenantCampaigns.get(campaign.tenantId) || [];
      existing.push(campaign.id);
      tenantCampaigns.set(campaign.tenantId, existing);
    }

    // Check tenant-level limits
    for (const [tenantId, campaignIds] of tenantCampaigns) {
      const maxConcurrentCampaigns = await this.getTenantMaxConcurrentCampaigns(tenantId);

      if (campaignIds.length > maxConcurrentCampaigns) {
        this.logger.warn(
          `Tenant ${tenantId} exceeds max concurrent campaigns (${campaignIds.length}/${maxConcurrentCampaigns})`,
        );

        // Pause excess campaigns (keep the earliest ones running)
        const excessCampaigns = campaignIds.slice(maxConcurrentCampaigns);
        for (const campaignId of excessCampaigns) {
          await this.prisma.campaign.update({
            where: { id: campaignId },
            data: { status: CampaignStatus.PAUSED },
          });
          await this.campaignExecutor.pauseCampaign(campaignId);
          this.logger.log(`Rate-limited campaign ${campaignId} for tenant ${tenantId}`);
        }
      }
    }
  }

  /**
   * Checks if a tenant can start a new campaign based on their rate limits.
   */
  private async checkTenantCanStartCampaign(tenantId: string): Promise<boolean> {
    const runningCount = await this.prisma.campaign.count({
      where: { tenantId, status: CampaignStatus.RUNNING },
    });

    const maxConcurrent = await this.getTenantMaxConcurrentCampaigns(tenantId);
    return runningCount < maxConcurrent;
  }

  /**
   * Gets the maximum number of concurrent campaigns for a tenant.
   */
  private async getTenantMaxConcurrentCampaigns(tenantId: string): Promise<number> {
    // Check Redis cache first
    const cacheKey = `tenant:limits:maxCampaigns:${tenantId}`;
    const cached = await this.redisService.get(cacheKey);
    if (cached) return parseInt(cached, 10);

    // Default limit, can be overridden per tenant in the database
    const tenant = await this.prisma.tenant.findUnique({
      where: { id: tenantId },
      select: { settings: true },
    });

    const settings = (tenant?.settings as Record<string, unknown>) || {};
    const maxCampaigns = (settings.maxConcurrentCampaigns as number) || 5;

    // Cache for 5 minutes
    await this.redisService.set(cacheKey, String(maxCampaigns), 300);
    return maxCampaigns;
  }

  // ─── HELPERS ──────────────────────────────────────────────────────────────────

  private isWithinCallingWindow(
    startHour: number,
    endHour: number,
    timezone: string,
    daysOfWeek: number[],
  ): boolean {
    const now = new Date();

    try {
      const formatter = new Intl.DateTimeFormat('en-US', {
        timeZone: timezone,
        hour: 'numeric',
        hour12: false,
        weekday: 'short',
      });

      const parts = formatter.formatToParts(now);
      const hourPart = parts.find((p) => p.type === 'hour');
      const dayPart = parts.find((p) => p.type === 'weekday');

      const currentHour = parseInt(hourPart?.value || '0', 10);
      const dayMap: Record<string, number> = {
        Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6,
      };
      const currentDay = dayMap[dayPart?.value || ''] ?? now.getDay();

      if (!daysOfWeek.includes(currentDay)) return false;
      return currentHour >= startHour && currentHour < endHour;
    } catch (error) {
      this.logger.warn(`Invalid timezone "${timezone}": ${(error as Error).message}`);
      return true; // Default to allowing if timezone is invalid
    }
  }

  /**
   * Acquires a distributed lock using Redis SET NX EX.
   */
  private async acquireLock(key: string, ttlSeconds: number): Promise<boolean> {
    const client = this.redisService.getClient();
    const result = await client.set(key, process.pid.toString(), 'EX', ttlSeconds, 'NX');
    return result === 'OK';
  }

  /**
   * Releases a distributed lock.
   */
  private async releaseLock(key: string): Promise<void> {
    await this.redisService.del(key);
  }
}
