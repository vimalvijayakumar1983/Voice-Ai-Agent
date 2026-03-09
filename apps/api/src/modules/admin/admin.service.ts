import { Injectable, Logger } from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';
import { RedisService } from '../../common/services/redis.service';
import { PaginationQuery, PaginatedResponse } from '../../common/dto/pagination.dto';

@Injectable()
export class AdminService {
  private readonly logger = new Logger(AdminService.name);

  constructor(
    private readonly prisma: PrismaService,
    private readonly redisService: RedisService,
  ) {}

  async listTenants(query: PaginationQuery): Promise<PaginatedResponse<any>> {
    const { page, limit, sortBy, sortOrder } = query;
    const skip = (page - 1) * limit;

    const [tenants, total] = await Promise.all([
      this.prisma.tenant.findMany({
        skip,
        take: limit,
        orderBy: { [sortBy || 'createdAt']: sortOrder || 'desc' },
        include: {
          _count: {
            select: { users: true, agents: true, campaigns: true, calls: true },
          },
        },
      }),
      this.prisma.tenant.count(),
    ]);

    return new PaginatedResponse(tenants, total, query);
  }

  async listAllUsers(query: PaginationQuery): Promise<PaginatedResponse<any>> {
    const { page, limit, sortBy, sortOrder } = query;
    const skip = (page - 1) * limit;

    const [users, total] = await Promise.all([
      this.prisma.user.findMany({
        skip,
        take: limit,
        orderBy: { [sortBy || 'createdAt']: sortOrder || 'desc' },
        select: {
          id: true,
          email: true,
          firstName: true,
          lastName: true,
          role: true,
          isActive: true,
          tenantId: true,
          tenant: { select: { name: true } },
          createdAt: true,
          lastLoginAt: true,
        },
      }),
      this.prisma.user.count(),
    ]);

    return new PaginatedResponse(users, total, query);
  }

  async getFeatureFlags(): Promise<Record<string, boolean>> {
    const cached = await this.redisService.getJson<Record<string, boolean>>('feature-flags');
    if (cached) return cached;

    // Default feature flags
    const flags: Record<string, boolean> = {
      enableVoiceCloning: false,
      enableAdvancedAnalytics: true,
      enableBulkCampaigns: true,
      enableCustomIntegrations: true,
      enableKnowledgeBase: true,
      enableMultiLanguage: false,
      enableWhisperSTT: true,
      enableSentimentAnalysis: true,
      maintenanceMode: false,
    };

    await this.redisService.setJson('feature-flags', flags, 3600);
    return flags;
  }

  async updateFeatureFlag(key: string, value: boolean): Promise<Record<string, boolean>> {
    const flags = await this.getFeatureFlags();
    flags[key] = value;
    await this.redisService.setJson('feature-flags', flags, 3600);
    this.logger.log(`Feature flag "${key}" set to ${value}`);
    return flags;
  }

  async getSystemHealth(): Promise<any> {
    const checks: Record<string, { status: string; latencyMs?: number }> = {};

    // Database check
    const dbStart = Date.now();
    try {
      await this.prisma.$queryRaw`SELECT 1`;
      checks.database = { status: 'healthy', latencyMs: Date.now() - dbStart };
    } catch {
      checks.database = { status: 'unhealthy', latencyMs: Date.now() - dbStart };
    }

    // Redis check
    const redisStart = Date.now();
    try {
      await this.redisService.set('health-check', '1', 10);
      checks.redis = { status: 'healthy', latencyMs: Date.now() - redisStart };
    } catch {
      checks.redis = { status: 'unhealthy', latencyMs: Date.now() - redisStart };
    }

    const allHealthy = Object.values(checks).every((c) => c.status === 'healthy');

    return {
      status: allHealthy ? 'healthy' : 'degraded',
      timestamp: new Date().toISOString(),
      uptime: process.uptime(),
      memoryUsage: process.memoryUsage(),
      checks,
    };
  }

  async getUsageOverview(): Promise<any> {
    const [
      totalTenants,
      totalUsers,
      totalCalls,
      totalAgents,
      activeCampaigns,
    ] = await Promise.all([
      this.prisma.tenant.count(),
      this.prisma.user.count(),
      this.prisma.call.count(),
      this.prisma.agent.count(),
      this.prisma.campaign.count({ where: { status: 'RUNNING' } }),
    ]);

    return {
      totalTenants,
      totalUsers,
      totalCalls,
      totalAgents,
      activeCampaigns,
      timestamp: new Date().toISOString(),
    };
  }
}
