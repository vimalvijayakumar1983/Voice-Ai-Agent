import { Injectable, Logger } from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';

@Injectable()
export class AnalyticsService {
  private readonly logger = new Logger(AnalyticsService.name);

  constructor(private readonly prisma: PrismaService) {}

  async getDashboardStats(tenantId: string): Promise<any> {
    const [
      totalCalls,
      totalContacts,
      totalAgents,
      activeCampaigns,
      callsToday,
    ] = await Promise.all([
      this.prisma.call.count({ where: { tenantId } }),
      this.prisma.contact.count({ where: { tenantId } }),
      this.prisma.agent.count({ where: { tenantId, isActive: true } }),
      this.prisma.campaign.count({ where: { tenantId, status: 'RUNNING' } }),
      this.prisma.call.count({
        where: {
          tenantId,
          startedAt: { gte: new Date(new Date().setHours(0, 0, 0, 0)) },
        },
      }),
    ]);

    return {
      totalCalls,
      totalContacts,
      totalAgents,
      activeCampaigns,
      callsToday,
    };
  }

  async getCallMetrics(
    tenantId: string,
    dateFrom: string,
    dateTo: string,
  ): Promise<any> {
    const where = {
      tenantId,
      startedAt: {
        gte: new Date(dateFrom),
        lte: new Date(dateTo),
      },
    };

    const [statusBreakdown, avgDuration, totalCalls] = await Promise.all([
      this.prisma.call.groupBy({
        by: ['status'],
        where,
        _count: { id: true },
      }),
      this.prisma.call.aggregate({
        where: { ...where, status: 'COMPLETED' },
        _avg: { durationSeconds: true },
        _sum: { durationSeconds: true },
      }),
      this.prisma.call.count({ where }),
    ]);

    return {
      totalCalls,
      statusBreakdown: statusBreakdown.reduce(
        (acc, s) => ({ ...acc, [s.status]: s._count.id }),
        {},
      ),
      averageDurationSeconds: avgDuration._avg.durationSeconds || 0,
      totalDurationSeconds: avgDuration._sum.durationSeconds || 0,
    };
  }

  async getCampaignMetrics(tenantId: string): Promise<any> {
    const campaigns = await this.prisma.campaign.findMany({
      where: { tenantId },
      select: {
        id: true,
        name: true,
        status: true,
        type: true,
        _count: { select: { calls: true } },
      },
      orderBy: { createdAt: 'desc' },
      take: 10,
    });

    const statusBreakdown = await this.prisma.campaign.groupBy({
      by: ['status'],
      where: { tenantId },
      _count: { id: true },
    });

    return {
      recentCampaigns: campaigns,
      statusBreakdown: statusBreakdown.reduce(
        (acc, s) => ({ ...acc, [s.status]: s._count.id }),
        {},
      ),
    };
  }

  async getAgentPerformance(tenantId: string): Promise<any> {
    const agents = await this.prisma.agent.findMany({
      where: { tenantId, isActive: true },
      select: {
        id: true,
        name: true,
        type: true,
        _count: { select: { calls: true } },
      },
    });

    const agentMetrics = await Promise.all(
      agents.map(async (agent) => {
        const callStats = await this.prisma.call.aggregate({
          where: { agentId: agent.id, status: 'COMPLETED' },
          _avg: { durationSeconds: true, sentimentScore: true },
          _count: { id: true },
        });

        return {
          agentId: agent.id,
          agentName: agent.name,
          totalCalls: agent._count.calls,
          completedCalls: callStats._count.id,
          averageDurationSeconds: callStats._avg.durationSeconds || 0,
          averageSentimentScore: callStats._avg.sentimentScore || 0,
        };
      }),
    );

    return agentMetrics;
  }

  async getTenantUsage(tenantId: string): Promise<any> {
    const now = new Date();
    const startOfMonth = new Date(now.getFullYear(), now.getMonth(), 1);

    const [
      callsThisMonth,
      totalMinutes,
      activeUsers,
    ] = await Promise.all([
      this.prisma.call.count({
        where: { tenantId, startedAt: { gte: startOfMonth } },
      }),
      this.prisma.call.aggregate({
        where: { tenantId, startedAt: { gte: startOfMonth }, status: 'COMPLETED' },
        _sum: { durationSeconds: true },
      }),
      this.prisma.user.count({
        where: { tenantId, isActive: true },
      }),
    ]);

    return {
      period: {
        start: startOfMonth.toISOString(),
        end: now.toISOString(),
      },
      callsThisMonth,
      totalMinutes: Math.round((totalMinutes._sum.durationSeconds || 0) / 60),
      activeUsers,
    };
  }
}
