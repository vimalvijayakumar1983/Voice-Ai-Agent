import { Injectable, Logger } from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';
import { RedisService } from '../../common/services/redis.service';

// ============================================================================
// Types
// ============================================================================

export interface AggregatedMetrics {
  period: { start: string; end: string; granularity: string };
  callVolume: CallVolumeMetrics;
  successRates: SuccessRateMetrics;
  duration: DurationMetrics;
  sentiment: SentimentMetrics;
  cost: CostMetrics;
}

export interface CallVolumeMetrics {
  total: number;
  completed: number;
  failed: number;
  noAnswer: number;
  busy: number;
  voicemail: number;
  transferred: number;
  byHour: Array<{ hour: number; count: number }>;
  byDay: Array<{ date: string; count: number }>;
}

export interface SuccessRateMetrics {
  overall: number;
  byAgent: Array<{ agentId: string; agentName: string; rate: number; total: number }>;
  byCampaign: Array<{ campaignId: string; campaignName: string; rate: number; total: number }>;
  byOutcome: Record<string, number>;
}

export interface DurationMetrics {
  average: number;
  median: number;
  p95: number;
  min: number;
  max: number;
  total: number;
}

export interface SentimentMetrics {
  averageScore: number;
  positive: number;
  neutral: number;
  negative: number;
  byHourAndDay: Array<{ dayOfWeek: number; hour: number; avgSentiment: number; count: number }>;
  trend: Array<{ date: string; avgSentiment: number }>;
}

export interface CostMetrics {
  total: number;
  byType: Record<string, number>;
  perCall: number;
  perMinute: number;
  trend: Array<{ date: string; cost: number }>;
}

export interface AgentPerformanceScore {
  agentId: string;
  agentName: string;
  totalCalls: number;
  completedCalls: number;
  successRate: number;
  avgDuration: number;
  avgSentiment: number;
  score: number;
  rank: number;
  trend: number; // percentage change vs previous period
}

export interface CampaignFunnel {
  campaignId: string;
  campaignName: string;
  totalContacts: number;
  called: number;
  answered: number;
  completed: number;
  converted: number;
  conversionRates: {
    calledRate: number;
    answerRate: number;
    completionRate: number;
    conversionRate: number;
  };
}

export interface CohortAnalysis {
  segment: string;
  contactCount: number;
  callCount: number;
  avgCallsPerContact: number;
  successRate: number;
  avgSentiment: number;
}

export interface RealtimeSnapshot {
  activeCalls: number;
  callsPerMinute: number;
  avgWaitTime: number;
  successRateLast15Min: number;
  queuedCalls: number;
  agentsActive: number;
  timestamp: string;
}

// ============================================================================
// Service
// ============================================================================

@Injectable()
export class AnalyticsAggregatorService {
  private readonly logger = new Logger(AnalyticsAggregatorService.name);
  private readonly CACHE_PREFIX = 'analytics:';
  private readonly CACHE_TTL_HOURLY = 3600;     // 1 hour
  private readonly CACHE_TTL_DAILY = 86400;     // 24 hours
  private readonly CACHE_TTL_MONTHLY = 604800;  // 7 days

  constructor(
    private readonly prisma: PrismaService,
    private readonly redis: RedisService,
  ) {}

  // --------------------------------------------------------------------------
  // Aggregation Runners
  // --------------------------------------------------------------------------

  async aggregateHourly(tenantId: string): Promise<AggregatedMetrics> {
    const now = new Date();
    const start = new Date(now.getTime() - 60 * 60 * 1000);
    return this.aggregate(tenantId, start, now, 'hourly');
  }

  async aggregateDaily(tenantId: string): Promise<AggregatedMetrics> {
    const now = new Date();
    const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return this.aggregate(tenantId, start, now, 'daily');
  }

  async aggregateMonthly(tenantId: string): Promise<AggregatedMetrics> {
    const now = new Date();
    const start = new Date(now.getFullYear(), now.getMonth(), 1);
    return this.aggregate(tenantId, start, now, 'monthly');
  }

  async aggregateRange(
    tenantId: string,
    from: Date,
    to: Date,
    granularity: 'hourly' | 'daily' | 'weekly' | 'monthly' = 'daily',
  ): Promise<AggregatedMetrics> {
    return this.aggregate(tenantId, from, to, granularity);
  }

  // --------------------------------------------------------------------------
  // Core Aggregation
  // --------------------------------------------------------------------------

  private async aggregate(
    tenantId: string,
    from: Date,
    to: Date,
    granularity: string,
  ): Promise<AggregatedMetrics> {
    const cacheKey = `${this.CACHE_PREFIX}${tenantId}:${granularity}:${from.toISOString()}:${to.toISOString()}`;
    const cached = await this.redis.getJson<AggregatedMetrics>(cacheKey);
    if (cached) return cached;

    const where = {
      tenantId,
      createdAt: { gte: from, lte: to },
    };

    const [callVolume, successRates, duration, sentiment, cost] = await Promise.all([
      this.aggregateCallVolume(tenantId, from, to),
      this.aggregateSuccessRates(tenantId, from, to),
      this.aggregateDuration(tenantId, from, to),
      this.aggregateSentiment(tenantId, from, to),
      this.aggregateCosts(tenantId, from, to),
    ]);

    const result: AggregatedMetrics = {
      period: {
        start: from.toISOString(),
        end: to.toISOString(),
        granularity,
      },
      callVolume,
      successRates,
      duration,
      sentiment,
      cost,
    };

    const ttl = granularity === 'hourly'
      ? this.CACHE_TTL_HOURLY
      : granularity === 'daily'
        ? this.CACHE_TTL_DAILY
        : this.CACHE_TTL_MONTHLY;
    await this.redis.setJson(cacheKey, result, ttl);

    return result;
  }

  // --------------------------------------------------------------------------
  // Call Volume
  // --------------------------------------------------------------------------

  private async aggregateCallVolume(
    tenantId: string,
    from: Date,
    to: Date,
  ): Promise<CallVolumeMetrics> {
    const where = { tenantId, createdAt: { gte: from, lte: to } };

    const [statusCounts, total] = await Promise.all([
      this.prisma.call.groupBy({
        by: ['status'],
        where,
        _count: { id: true },
      }),
      this.prisma.call.count({ where }),
    ]);

    const statusMap = statusCounts.reduce(
      (acc, s) => ({ ...acc, [s.status]: s._count.id }),
      {} as Record<string, number>,
    );

    // Calls by hour (use raw query for grouping)
    const callsByHour = await this.getCallsByHour(tenantId, from, to);
    const callsByDay = await this.getCallsByDay(tenantId, from, to);

    return {
      total,
      completed: statusMap['COMPLETED'] || 0,
      failed: statusMap['FAILED'] || 0,
      noAnswer: statusMap['NO_ANSWER'] || 0,
      busy: statusMap['BUSY'] || 0,
      voicemail: statusMap['VOICEMAIL'] || 0,
      transferred: statusMap['TRANSFERRED'] || 0,
      byHour: callsByHour,
      byDay: callsByDay,
    };
  }

  private async getCallsByHour(
    tenantId: string,
    from: Date,
    to: Date,
  ): Promise<Array<{ hour: number; count: number }>> {
    try {
      const result = await this.prisma.$queryRaw<Array<{ hour: number; count: bigint }>>`
        SELECT EXTRACT(HOUR FROM "createdAt")::int as hour, COUNT(*)::bigint as count
        FROM calls
        WHERE "tenantId" = ${tenantId}::uuid
          AND "createdAt" >= ${from}
          AND "createdAt" <= ${to}
        GROUP BY EXTRACT(HOUR FROM "createdAt")
        ORDER BY hour
      `;
      return result.map((r) => ({ hour: r.hour, count: Number(r.count) }));
    } catch (error) {
      this.logger.warn(`Failed to get calls by hour: ${error}`);
      return [];
    }
  }

  private async getCallsByDay(
    tenantId: string,
    from: Date,
    to: Date,
  ): Promise<Array<{ date: string; count: number }>> {
    try {
      const result = await this.prisma.$queryRaw<Array<{ date: string; count: bigint }>>`
        SELECT DATE("createdAt")::text as date, COUNT(*)::bigint as count
        FROM calls
        WHERE "tenantId" = ${tenantId}::uuid
          AND "createdAt" >= ${from}
          AND "createdAt" <= ${to}
        GROUP BY DATE("createdAt")
        ORDER BY date
      `;
      return result.map((r) => ({ date: r.date, count: Number(r.count) }));
    } catch (error) {
      this.logger.warn(`Failed to get calls by day: ${error}`);
      return [];
    }
  }

  // --------------------------------------------------------------------------
  // Success Rates
  // --------------------------------------------------------------------------

  private async aggregateSuccessRates(
    tenantId: string,
    from: Date,
    to: Date,
  ): Promise<SuccessRateMetrics> {
    const where = { tenantId, createdAt: { gte: from, lte: to } };

    const [total, completed, outcomeBreakdown] = await Promise.all([
      this.prisma.call.count({ where }),
      this.prisma.call.count({ where: { ...where, status: 'COMPLETED' } }),
      this.prisma.call.groupBy({
        by: ['outcome'],
        where: { ...where, outcome: { not: null } },
        _count: { id: true },
      }),
    ]);

    const overall = total > 0 ? (completed / total) * 100 : 0;

    // By Agent
    const agentStats = await this.prisma.call.groupBy({
      by: ['agentId'],
      where,
      _count: { id: true },
    });

    const agentSuccessStats = await this.prisma.call.groupBy({
      by: ['agentId'],
      where: { ...where, status: 'COMPLETED' },
      _count: { id: true },
    });

    const agentNames = await this.prisma.agent.findMany({
      where: { tenantId },
      select: { id: true, name: true },
    });
    const agentNameMap = new Map(agentNames.map((a) => [a.id, a.name]));

    const successByAgent = new Map(agentSuccessStats.map((s) => [s.agentId, s._count.id]));
    const byAgent = agentStats.map((s) => ({
      agentId: s.agentId,
      agentName: agentNameMap.get(s.agentId) || 'Unknown',
      rate: s._count.id > 0 ? ((successByAgent.get(s.agentId) || 0) / s._count.id) * 100 : 0,
      total: s._count.id,
    }));

    // By Campaign
    const campaignStats = await this.prisma.call.groupBy({
      by: ['campaignId'],
      where: { ...where, campaignId: { not: null } },
      _count: { id: true },
    });

    const campaignSuccessStats = await this.prisma.call.groupBy({
      by: ['campaignId'],
      where: { ...where, campaignId: { not: null }, status: 'COMPLETED' },
      _count: { id: true },
    });

    const campaignNames = await this.prisma.campaign.findMany({
      where: { tenantId },
      select: { id: true, name: true },
    });
    const campaignNameMap = new Map(campaignNames.map((c) => [c.id, c.name]));

    const successByCampaign = new Map(
      campaignSuccessStats.map((s) => [s.campaignId, s._count.id]),
    );
    const byCampaign = campaignStats
      .filter((s) => s.campaignId)
      .map((s) => ({
        campaignId: s.campaignId!,
        campaignName: campaignNameMap.get(s.campaignId!) || 'Unknown',
        rate: s._count.id > 0 ? (Number(successByCampaign.get(s.campaignId) || 0) / s._count.id) * 100 : 0,
        total: s._count.id,
      }));

    const byOutcome = outcomeBreakdown.reduce(
      (acc, o) => ({ ...acc, [o.outcome || 'unknown']: o._count.id }),
      {} as Record<string, number>,
    );

    return { overall, byAgent, byCampaign, byOutcome };
  }

  // --------------------------------------------------------------------------
  // Duration
  // --------------------------------------------------------------------------

  private async aggregateDuration(
    tenantId: string,
    from: Date,
    to: Date,
  ): Promise<DurationMetrics> {
    const where = {
      tenantId,
      createdAt: { gte: from, lte: to },
      status: 'COMPLETED' as const,
      duration: { not: null },
    };

    const agg = await this.prisma.call.aggregate({
      where,
      _avg: { duration: true },
      _min: { duration: true },
      _max: { duration: true },
      _sum: { duration: true },
      _count: { id: true },
    });

    // Compute percentiles via raw query
    let median = 0;
    let p95 = 0;
    try {
      const percentiles = await this.prisma.$queryRaw<Array<{ median: number; p95: number }>>`
        SELECT
          COALESCE(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY duration), 0) as median,
          COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration), 0) as p95
        FROM calls
        WHERE "tenantId" = ${tenantId}::uuid
          AND "createdAt" >= ${from}
          AND "createdAt" <= ${to}
          AND status = 'COMPLETED'
          AND duration IS NOT NULL
      `;
      if (percentiles.length > 0) {
        median = Number(percentiles[0].median);
        p95 = Number(percentiles[0].p95);
      }
    } catch (error) {
      this.logger.warn(`Percentile query failed: ${error}`);
      median = agg._avg.duration || 0;
      p95 = agg._max.duration || 0;
    }

    return {
      average: agg._avg.duration || 0,
      median,
      p95,
      min: agg._min.duration || 0,
      max: agg._max.duration || 0,
      total: agg._sum.duration || 0,
    };
  }

  // --------------------------------------------------------------------------
  // Sentiment
  // --------------------------------------------------------------------------

  private async aggregateSentiment(
    tenantId: string,
    from: Date,
    to: Date,
  ): Promise<SentimentMetrics> {
    const where = {
      tenantId,
      createdAt: { gte: from, lte: to },
      sentiment: { not: null },
    };

    const calls = await this.prisma.call.findMany({
      where,
      select: { sentiment: true, createdAt: true },
    });

    let totalScore = 0;
    let positive = 0;
    let neutral = 0;
    let negative = 0;

    const sentimentMap: Record<string, number> = {
      positive: 1,
      neutral: 0,
      negative: -1,
    };

    for (const call of calls) {
      const s = call.sentiment?.toLowerCase() || 'neutral';
      const score = sentimentMap[s] ?? 0;
      totalScore += score;
      if (s === 'positive') positive++;
      else if (s === 'negative') negative++;
      else neutral++;
    }

    const averageScore = calls.length > 0 ? totalScore / calls.length : 0;

    // Sentiment by hour and day of week
    const byHourAndDay: Array<{ dayOfWeek: number; hour: number; avgSentiment: number; count: number }> = [];
    const hourDayMap = new Map<string, { total: number; count: number }>();

    for (const call of calls) {
      const date = new Date(call.createdAt);
      const key = `${date.getDay()}-${date.getHours()}`;
      const existing = hourDayMap.get(key) || { total: 0, count: 0 };
      const s = call.sentiment?.toLowerCase() || 'neutral';
      existing.total += sentimentMap[s] ?? 0;
      existing.count += 1;
      hourDayMap.set(key, existing);
    }

    for (const [key, value] of hourDayMap) {
      const [day, hour] = key.split('-').map(Number);
      byHourAndDay.push({
        dayOfWeek: day,
        hour,
        avgSentiment: value.count > 0 ? value.total / value.count : 0,
        count: value.count,
      });
    }

    // Sentiment trend by day
    const dayMap = new Map<string, { total: number; count: number }>();
    for (const call of calls) {
      const dateKey = new Date(call.createdAt).toISOString().split('T')[0];
      const existing = dayMap.get(dateKey) || { total: 0, count: 0 };
      const s = call.sentiment?.toLowerCase() || 'neutral';
      existing.total += sentimentMap[s] ?? 0;
      existing.count += 1;
      dayMap.set(dateKey, existing);
    }

    const trend = Array.from(dayMap.entries())
      .map(([date, value]) => ({
        date,
        avgSentiment: value.count > 0 ? value.total / value.count : 0,
      }))
      .sort((a, b) => a.date.localeCompare(b.date));

    return { averageScore, positive, neutral, negative, byHourAndDay, trend };
  }

  // --------------------------------------------------------------------------
  // Cost Analysis
  // --------------------------------------------------------------------------

  private async aggregateCosts(
    tenantId: string,
    from: Date,
    to: Date,
  ): Promise<CostMetrics> {
    const usageRecords = await this.prisma.usageRecord.findMany({
      where: {
        tenantId,
        periodStart: { gte: from },
        periodEnd: { lte: to },
      },
    });

    let total = 0;
    const byType: Record<string, number> = {};

    for (const record of usageRecords) {
      total += record.cost;
      const typeKey = record.type.toLowerCase();
      byType[typeKey] = (byType[typeKey] || 0) + record.cost;
    }

    const callCount = await this.prisma.call.count({
      where: { tenantId, createdAt: { gte: from, lte: to } },
    });

    const totalMinutes = await this.prisma.call.aggregate({
      where: { tenantId, createdAt: { gte: from, lte: to }, status: 'COMPLETED' },
      _sum: { duration: true },
    });

    const minutes = (totalMinutes._sum.duration || 0) / 60;

    // Cost trend by day
    const dayMap = new Map<string, number>();
    for (const record of usageRecords) {
      const dateKey = new Date(record.periodStart).toISOString().split('T')[0];
      dayMap.set(dateKey, (dayMap.get(dateKey) || 0) + record.cost);
    }

    const costTrend = Array.from(dayMap.entries())
      .map(([date, cost]) => ({ date, cost }))
      .sort((a, b) => a.date.localeCompare(b.date));

    return {
      total,
      byType,
      perCall: callCount > 0 ? total / callCount : 0,
      perMinute: minutes > 0 ? total / minutes : 0,
      trend: costTrend,
    };
  }

  // --------------------------------------------------------------------------
  // Agent Performance Scoring
  // --------------------------------------------------------------------------

  async getAgentPerformanceScores(
    tenantId: string,
    from: Date,
    to: Date,
  ): Promise<AgentPerformanceScore[]> {
    const cacheKey = `${this.CACHE_PREFIX}${tenantId}:agent-scores:${from.toISOString()}:${to.toISOString()}`;
    const cached = await this.redis.getJson<AgentPerformanceScore[]>(cacheKey);
    if (cached) return cached;

    const agents = await this.prisma.agent.findMany({
      where: { tenantId, isActive: true },
      select: { id: true, name: true },
    });

    const where = { tenantId, createdAt: { gte: from, lte: to } };

    // Previous period for trend
    const periodLength = to.getTime() - from.getTime();
    const prevFrom = new Date(from.getTime() - periodLength);
    const prevWhere = { tenantId, createdAt: { gte: prevFrom, lte: from } };

    const scores: AgentPerformanceScore[] = [];

    for (const agent of agents) {
      const [current, previous] = await Promise.all([
        this.getAgentMetrics(agent.id, where),
        this.getAgentMetrics(agent.id, prevWhere),
      ]);

      // Weighted score: success rate (40%), sentiment (30%), efficiency (30%)
      const successWeight = 0.4;
      const sentimentWeight = 0.3;
      const efficiencyWeight = 0.3;

      const normalizedSentiment = ((current.avgSentiment + 1) / 2) * 100; // -1..1 -> 0..100
      const normalizedEfficiency = current.avgDuration > 0
        ? Math.min(100, (300 / current.avgDuration) * 100) // 5 min target = 100%
        : 0;

      const score =
        current.successRate * successWeight +
        normalizedSentiment * sentimentWeight +
        normalizedEfficiency * efficiencyWeight;

      const prevScore =
        previous.successRate * successWeight +
        (((previous.avgSentiment + 1) / 2) * 100) * sentimentWeight +
        (previous.avgDuration > 0
          ? Math.min(100, (300 / previous.avgDuration) * 100)
          : 0) *
          efficiencyWeight;

      const trend = prevScore > 0 ? ((score - prevScore) / prevScore) * 100 : 0;

      scores.push({
        agentId: agent.id,
        agentName: agent.name,
        totalCalls: current.totalCalls,
        completedCalls: current.completedCalls,
        successRate: current.successRate,
        avgDuration: current.avgDuration,
        avgSentiment: current.avgSentiment,
        score: Math.round(score * 10) / 10,
        rank: 0, // Set after sorting
        trend: Math.round(trend * 10) / 10,
      });
    }

    // Assign ranks
    scores.sort((a, b) => b.score - a.score);
    scores.forEach((s, i) => (s.rank = i + 1));

    await this.redis.setJson(cacheKey, scores, this.CACHE_TTL_HOURLY);
    return scores;
  }

  private async getAgentMetrics(
    agentId: string,
    where: Record<string, unknown>,
  ): Promise<{
    totalCalls: number;
    completedCalls: number;
    successRate: number;
    avgDuration: number;
    avgSentiment: number;
  }> {
    const agentWhere = { ...where, agentId };

    const [total, completed, agg] = await Promise.all([
      this.prisma.call.count({ where: agentWhere }),
      this.prisma.call.count({ where: { ...agentWhere, status: 'COMPLETED' } }),
      this.prisma.call.aggregate({
        where: { ...agentWhere, status: 'COMPLETED' },
        _avg: { duration: true },
      }),
    ]);

    // Get sentiment (stored as string, not score)
    const sentimentCalls = await this.prisma.call.findMany({
      where: { ...agentWhere, sentiment: { not: null } },
      select: { sentiment: true },
    });

    const sentimentMap: Record<string, number> = { positive: 1, neutral: 0, negative: -1 };
    let sentimentTotal = 0;
    for (const c of sentimentCalls) {
      sentimentTotal += sentimentMap[c.sentiment?.toLowerCase() || 'neutral'] ?? 0;
    }

    return {
      totalCalls: total,
      completedCalls: completed,
      successRate: total > 0 ? (completed / total) * 100 : 0,
      avgDuration: agg._avg.duration || 0,
      avgSentiment: sentimentCalls.length > 0 ? sentimentTotal / sentimentCalls.length : 0,
    };
  }

  // --------------------------------------------------------------------------
  // Campaign Funnel
  // --------------------------------------------------------------------------

  async getCampaignFunnel(
    tenantId: string,
    campaignId: string,
  ): Promise<CampaignFunnel> {
    const campaign = await this.prisma.campaign.findFirst({
      where: { id: campaignId, tenantId },
      select: { id: true, name: true, contactListFilter: true },
    });

    if (!campaign) {
      return {
        campaignId,
        campaignName: 'Unknown',
        totalContacts: 0,
        called: 0,
        answered: 0,
        completed: 0,
        converted: 0,
        conversionRates: { calledRate: 0, answerRate: 0, completionRate: 0, conversionRate: 0 },
      };
    }

    const totalContacts = await this.prisma.contact.count({
      where: { tenantId },
    });

    const callStats = await this.prisma.call.groupBy({
      by: ['status'],
      where: { campaignId, tenantId },
      _count: { id: true },
    });

    const statusMap = callStats.reduce(
      (acc, s) => ({ ...acc, [s.status]: s._count.id }),
      {} as Record<string, number>,
    );

    const called = callStats.reduce((sum, s) => sum + s._count.id, 0);
    const answered =
      (statusMap['COMPLETED'] || 0) +
      (statusMap['IN_PROGRESS'] || 0) +
      (statusMap['TRANSFERRED'] || 0);
    const completed = statusMap['COMPLETED'] || 0;

    // "Converted" = calls with a positive outcome
    const converted = await this.prisma.call.count({
      where: {
        campaignId,
        tenantId,
        status: 'COMPLETED',
        outcome: { in: ['converted', 'booked', 'qualified', 'sold'] },
      },
    });

    return {
      campaignId,
      campaignName: campaign.name,
      totalContacts,
      called,
      answered,
      completed,
      converted,
      conversionRates: {
        calledRate: totalContacts > 0 ? (called / totalContacts) * 100 : 0,
        answerRate: called > 0 ? (answered / called) * 100 : 0,
        completionRate: answered > 0 ? (completed / answered) * 100 : 0,
        conversionRate: completed > 0 ? (converted / completed) * 100 : 0,
      },
    };
  }

  // --------------------------------------------------------------------------
  // Cohort Analysis
  // --------------------------------------------------------------------------

  async getCohortAnalysis(
    tenantId: string,
    from: Date,
    to: Date,
  ): Promise<CohortAnalysis[]> {
    const segments = await this.prisma.contact.groupBy({
      by: ['segment'],
      where: { tenantId, segment: { not: null } },
      _count: { id: true },
    });

    const cohorts: CohortAnalysis[] = [];

    for (const seg of segments) {
      if (!seg.segment) continue;

      const contacts = await this.prisma.contact.findMany({
        where: { tenantId, segment: seg.segment },
        select: { id: true },
      });
      const contactIds = contacts.map((c) => c.id);

      const [callCount, completedCount, sentimentCalls] = await Promise.all([
        this.prisma.call.count({
          where: {
            tenantId,
            contactId: { in: contactIds },
            createdAt: { gte: from, lte: to },
          },
        }),
        this.prisma.call.count({
          where: {
            tenantId,
            contactId: { in: contactIds },
            createdAt: { gte: from, lte: to },
            status: 'COMPLETED',
          },
        }),
        this.prisma.call.findMany({
          where: {
            tenantId,
            contactId: { in: contactIds },
            createdAt: { gte: from, lte: to },
            sentiment: { not: null },
          },
          select: { sentiment: true },
        }),
      ]);

      const sentimentMap: Record<string, number> = { positive: 1, neutral: 0, negative: -1 };
      let sentimentTotal = 0;
      for (const c of sentimentCalls) {
        sentimentTotal += sentimentMap[c.sentiment?.toLowerCase() || 'neutral'] ?? 0;
      }

      cohorts.push({
        segment: seg.segment,
        contactCount: seg._count.id,
        callCount,
        avgCallsPerContact: seg._count.id > 0 ? callCount / seg._count.id : 0,
        successRate: callCount > 0 ? (completedCount / callCount) * 100 : 0,
        avgSentiment: sentimentCalls.length > 0 ? sentimentTotal / sentimentCalls.length : 0,
      });
    }

    return cohorts;
  }

  // --------------------------------------------------------------------------
  // Real-time Snapshot
  // --------------------------------------------------------------------------

  async getRealtimeSnapshot(tenantId: string): Promise<RealtimeSnapshot> {
    const now = new Date();
    const fifteenMinAgo = new Date(now.getTime() - 15 * 60 * 1000);
    const oneMinAgo = new Date(now.getTime() - 60 * 1000);

    const [activeCalls, callsLastMin, recentCalls, queuedCalls, agents] =
      await Promise.all([
        this.prisma.call.count({
          where: { tenantId, status: 'IN_PROGRESS' },
        }),
        this.prisma.call.count({
          where: { tenantId, createdAt: { gte: oneMinAgo } },
        }),
        this.prisma.call.findMany({
          where: { tenantId, createdAt: { gte: fifteenMinAgo } },
          select: { status: true, duration: true },
        }),
        this.prisma.call.count({
          where: { tenantId, status: 'QUEUED' },
        }),
        this.prisma.agent.count({
          where: { tenantId, isActive: true },
        }),
      ]);

    const completed15 = recentCalls.filter((c) => c.status === 'COMPLETED').length;
    const total15 = recentCalls.length;
    const successRate15 = total15 > 0 ? (completed15 / total15) * 100 : 0;

    const waitTimes = recentCalls
      .filter((c) => c.duration !== null)
      .map((c) => c.duration || 0);
    const avgWaitTime = waitTimes.length > 0
      ? waitTimes.reduce((a, b) => a + b, 0) / waitTimes.length
      : 0;

    return {
      activeCalls,
      callsPerMinute: callsLastMin,
      avgWaitTime,
      successRateLast15Min: Math.round(successRate15 * 10) / 10,
      queuedCalls,
      agentsActive: agents,
      timestamp: now.toISOString(),
    };
  }
}
