import { Injectable, Logger, ForbiddenException } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { PrismaService } from '../../common/services/prisma.service';
import { RedisService } from '../../common/services/redis.service';
import { StripeService } from './stripe.service';
import { PlansService, PlanTier } from './plans.service';

// ---------- Types ----------

export enum UsageDimension {
  CALL_MINUTES = 'call_minutes',
  API_CALLS = 'api_calls',
  TRANSCRIPTION_MINUTES = 'transcription_minutes',
  TTS_CHARACTERS = 'tts_characters',
  LLM_TOKENS = 'llm_tokens',
  STORAGE_GB = 'storage_gb',
}

export interface UsageRecord {
  tenantId: string;
  dimension: UsageDimension;
  quantity: number;
  timestamp: Date;
  metadata?: Record<string, unknown>;
}

export interface UsageSummary {
  dimension: UsageDimension;
  used: number;
  limit: number;
  percentage: number;
  unit: string;
}

export interface UsageAlert {
  tenantId: string;
  dimension: UsageDimension;
  threshold: number;
  currentUsage: number;
  limit: number;
  alertedAt: string;
}

const USAGE_UNITS: Record<UsageDimension, string> = {
  [UsageDimension.CALL_MINUTES]: 'minutes',
  [UsageDimension.API_CALLS]: 'calls',
  [UsageDimension.TRANSCRIPTION_MINUTES]: 'minutes',
  [UsageDimension.TTS_CHARACTERS]: 'characters',
  [UsageDimension.LLM_TOKENS]: 'tokens',
  [UsageDimension.STORAGE_GB]: 'GB',
};

const ALERT_THRESHOLDS = [0.8, 0.9, 1.0]; // 80%, 90%, 100%

// ---------- Service ----------

@Injectable()
export class UsageMeteringService {
  private readonly logger = new Logger(UsageMeteringService.name);
  private readonly flushIntervalMs: number;
  private flushTimer: NodeJS.Timeout | null = null;

  constructor(
    private readonly prisma: PrismaService,
    private readonly redisService: RedisService,
    private readonly stripeService: StripeService,
    private readonly plansService: PlansService,
    private readonly configService: ConfigService,
  ) {
    this.flushIntervalMs = this.configService.get<number>(
      'USAGE_FLUSH_INTERVAL_MS',
      60000,
    );

    // Start periodic flush
    this.startPeriodicFlush();
  }

  // ---------- Track Usage ----------

  async trackUsage(
    tenantId: string,
    dimension: UsageDimension,
    quantity: number,
    metadata?: Record<string, unknown>,
  ): Promise<void> {
    if (quantity <= 0) return;

    // Increment in Redis for fast accumulation
    const periodKey = this.getCurrentPeriodKey(tenantId, dimension);
    const client = this.redisService.getClient();

    // Use Redis INCRBYFLOAT for atomic increment
    const newValue = await client.incrbyfloat(periodKey, quantity);

    // Set expiry on first write (auto-cleanup after billing period)
    const ttl = await client.ttl(periodKey);
    if (ttl === -1) {
      // Set TTL to end of current month + 7 days buffer
      const endOfMonth = this.getEndOfCurrentMonth();
      const bufferMs = 7 * 24 * 60 * 60;
      const ttlSeconds = Math.ceil((endOfMonth.getTime() - Date.now()) / 1000) + bufferMs;
      await client.expire(periodKey, ttlSeconds);
    }

    // Track in a Redis sorted set for time-series granularity
    const timeseriesKey = `usage:ts:${tenantId}:${dimension}`;
    await client.zadd(
      timeseriesKey,
      Date.now(),
      JSON.stringify({
        quantity,
        timestamp: new Date().toISOString(),
        metadata,
      }),
    );

    // Check usage limits
    await this.checkUsageLimits(tenantId, dimension, parseFloat(newValue as unknown as string));
  }

  // ---------- Enforce Usage Limits ----------

  async enforceUsageLimit(
    tenantId: string,
    dimension: UsageDimension,
    requiredQuantity: number,
  ): Promise<boolean> {
    const currentUsage = await this.getCurrentUsage(tenantId, dimension);
    const limit = await this.getUsageLimit(tenantId, dimension);

    if (limit === -1) return true; // Unlimited

    if (currentUsage + requiredQuantity > limit) {
      this.logger.warn(
        `Tenant ${tenantId} exceeds ${dimension} limit: ${currentUsage + requiredQuantity}/${limit}`,
      );
      throw new ForbiddenException(
        `Usage limit exceeded for ${dimension}. Current: ${currentUsage}, Limit: ${limit}. Please upgrade your plan.`,
      );
    }

    return true;
  }

  // ---------- Query Usage ----------

  async getCurrentUsage(
    tenantId: string,
    dimension: UsageDimension,
  ): Promise<number> {
    const periodKey = this.getCurrentPeriodKey(tenantId, dimension);
    const value = await this.redisService.get(periodKey);
    return value ? parseFloat(value) : 0;
  }

  async getAllUsage(tenantId: string): Promise<UsageSummary[]> {
    const plan = await this.plansService.getTenantPlan(tenantId);
    const limits = this.plansService.getPlanLimits(plan);

    const summaries: UsageSummary[] = [];

    for (const dimension of Object.values(UsageDimension)) {
      const used = await this.getCurrentUsage(tenantId, dimension);
      const limit = limits[dimension] ?? -1;
      const percentage = limit > 0 ? (used / limit) * 100 : 0;

      summaries.push({
        dimension,
        used: Math.round(used * 100) / 100,
        limit,
        percentage: Math.round(percentage * 10) / 10,
        unit: USAGE_UNITS[dimension],
      });
    }

    return summaries;
  }

  async getUsageHistory(
    tenantId: string,
    dimension: UsageDimension,
    startDate: Date,
    endDate: Date,
  ): Promise<Array<{ date: string; quantity: number }>> {
    const timeseriesKey = `usage:ts:${tenantId}:${dimension}`;
    const client = this.redisService.getClient();

    const records = await client.zrangebyscore(
      timeseriesKey,
      startDate.getTime(),
      endDate.getTime(),
    );

    // Aggregate by day
    const dailyUsage = new Map<string, number>();

    for (const record of records) {
      try {
        const parsed = JSON.parse(record);
        const date = new Date(parsed.timestamp).toISOString().split('T')[0];
        dailyUsage.set(date, (dailyUsage.get(date) || 0) + parsed.quantity);
      } catch {
        continue;
      }
    }

    return Array.from(dailyUsage.entries())
      .map(([date, quantity]) => ({ date, quantity: Math.round(quantity * 100) / 100 }))
      .sort((a, b) => a.date.localeCompare(b.date));
  }

  // ---------- Usage Alerts ----------

  private async checkUsageLimits(
    tenantId: string,
    dimension: UsageDimension,
    currentUsage: number,
  ): Promise<void> {
    const limit = await this.getUsageLimit(tenantId, dimension);
    if (limit <= 0) return; // Unlimited

    const percentage = currentUsage / limit;

    for (const threshold of ALERT_THRESHOLDS) {
      if (percentage >= threshold) {
        const alertKey = `usage:alert:${tenantId}:${dimension}:${Math.round(threshold * 100)}`;
        const alreadyAlerted = await this.redisService.get(alertKey);

        if (!alreadyAlerted) {
          await this.sendUsageAlert(tenantId, dimension, threshold, currentUsage, limit);

          // Mark as alerted for this period
          const ttlSeconds = Math.ceil(
            (this.getEndOfCurrentMonth().getTime() - Date.now()) / 1000,
          );
          await this.redisService.set(alertKey, '1', ttlSeconds);
        }
      }
    }
  }

  private async sendUsageAlert(
    tenantId: string,
    dimension: UsageDimension,
    threshold: number,
    currentUsage: number,
    limit: number,
  ): Promise<void> {
    const alert: UsageAlert = {
      tenantId,
      dimension,
      threshold,
      currentUsage: Math.round(currentUsage),
      limit,
      alertedAt: new Date().toISOString(),
    };

    this.logger.warn(
      `Usage alert: tenant ${tenantId} at ${Math.round(threshold * 100)}% of ${dimension} limit (${currentUsage}/${limit})`,
    );

    // Publish alert via Redis for WebSocket delivery
    const client = this.redisService.getClient();
    await client.publish(
      'billing:usage:alert',
      JSON.stringify(alert),
    );

    // Store alert for retrieval
    await this.redisService.setJson(
      `usage:alert:latest:${tenantId}:${dimension}`,
      alert,
      86400 * 30,
    );
  }

  // ---------- Monthly Reset ----------

  async resetMonthlyUsage(tenantId: string): Promise<void> {
    this.logger.log(`Resetting monthly usage for tenant ${tenantId}`);

    const client = this.redisService.getClient();

    for (const dimension of Object.values(UsageDimension)) {
      if (dimension === UsageDimension.STORAGE_GB) continue; // Storage doesn't reset

      const periodKey = this.getCurrentPeriodKey(tenantId, dimension);
      await client.del(periodKey);

      // Clear alert flags
      for (const threshold of ALERT_THRESHOLDS) {
        await client.del(
          `usage:alert:${tenantId}:${dimension}:${Math.round(threshold * 100)}`,
        );
      }
    }
  }

  // ---------- Flush to Database ----------

  async flushUsageToDatabase(): Promise<void> {
    const client = this.redisService.getClient();

    // Find all usage period keys
    const keys = await client.keys('usage:period:*');

    for (const key of keys) {
      try {
        const parts = key.split(':');
        // usage:period:{tenantId}:{dimension}:{period}
        const tenantId = parts[2];
        const dimension = parts[3] as UsageDimension;
        const period = parts[4];

        const value = await client.get(key);
        if (!value) continue;

        const quantity = parseFloat(value);

        // Upsert to database
        await this.prisma.$executeRawUnsafe(
          `INSERT INTO "UsageRecord" ("tenantId", "dimension", "quantity", "period", "updatedAt")
           VALUES ($1, $2, $3, $4, NOW())
           ON CONFLICT ("tenantId", "dimension", "period")
           DO UPDATE SET "quantity" = $3, "updatedAt" = NOW()`,
          tenantId,
          dimension,
          quantity,
          period,
        );
      } catch (error) {
        this.logger.error(
          `Failed to flush usage for key ${key}: ${(error as Error).message}`,
        );
      }
    }
  }

  // ---------- Report to Stripe ----------

  async reportUsageToStripe(tenantId: string): Promise<void> {
    const tenant = await this.prisma.tenant.findUnique({ where: { id: tenantId } });
    if (!tenant) return;

    const metadata = (tenant.metadata as Record<string, unknown>) || {};
    const subscriptionId = metadata.stripeSubscriptionId as string;
    if (!subscriptionId) return;

    try {
      const subscription = await this.stripeService.getSubscription(subscriptionId);

      for (const item of subscription.items.data) {
        const meteredDimension = item.price.product; // Map product to dimension

        // Get current period usage
        for (const dimension of Object.values(UsageDimension)) {
          const usage = await this.getCurrentUsage(tenantId, dimension);
          if (usage > 0) {
            await this.stripeService.reportUsage(
              item.id,
              Math.ceil(usage),
              Math.floor(Date.now() / 1000),
              'set',
            );
          }
        }
      }

      this.logger.log(`Reported usage to Stripe for tenant ${tenantId}`);
    } catch (error) {
      this.logger.error(
        `Failed to report usage to Stripe for tenant ${tenantId}: ${(error as Error).message}`,
      );
    }
  }

  // ---------- Usage Export ----------

  async exportUsage(
    tenantId: string,
    startDate: Date,
    endDate: Date,
  ): Promise<Record<string, unknown>[]> {
    const rows: Record<string, unknown>[] = [];

    for (const dimension of Object.values(UsageDimension)) {
      const history = await this.getUsageHistory(tenantId, dimension, startDate, endDate);

      for (const entry of history) {
        rows.push({
          tenantId,
          dimension,
          unit: USAGE_UNITS[dimension],
          date: entry.date,
          quantity: entry.quantity,
        });
      }
    }

    return rows.sort((a, b) =>
      (a.date as string).localeCompare(b.date as string),
    );
  }

  // ---------- Helpers ----------

  private getCurrentPeriodKey(tenantId: string, dimension: UsageDimension): string {
    const period = this.getCurrentPeriod();
    return `usage:period:${tenantId}:${dimension}:${period}`;
  }

  private getCurrentPeriod(): string {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
  }

  private async getUsageLimit(
    tenantId: string,
    dimension: UsageDimension,
  ): Promise<number> {
    const plan = await this.plansService.getTenantPlan(tenantId);
    const limits = this.plansService.getPlanLimits(plan);
    return limits[dimension] ?? -1;
  }

  private getEndOfCurrentMonth(): Date {
    const now = new Date();
    return new Date(now.getFullYear(), now.getMonth() + 1, 1);
  }

  private startPeriodicFlush(): void {
    this.flushTimer = setInterval(async () => {
      try {
        await this.flushUsageToDatabase();
      } catch (error) {
        this.logger.error(
          `Periodic usage flush failed: ${(error as Error).message}`,
        );
      }
    }, this.flushIntervalMs);

    this.logger.log(
      `Usage flush started (interval: ${this.flushIntervalMs}ms)`,
    );
  }

  onModuleDestroy(): void {
    if (this.flushTimer) {
      clearInterval(this.flushTimer);
      this.flushTimer = null;
    }
  }
}
