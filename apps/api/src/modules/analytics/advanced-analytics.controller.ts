import {
  Controller,
  Get,
  Post,
  Body,
  Param,
  Query,
  UseGuards,
  Res,
} from '@nestjs/common';
import { ApiTags, ApiOperation, ApiBearerAuth, ApiQuery } from '@nestjs/swagger';
import { Response } from 'express';
import { JwtAuthGuard } from '../../common/guards/jwt-auth.guard';
import { RolesGuard } from '../../common/guards/roles.guard';
import { TenantGuard } from '../../common/guards/tenant.guard';
import { Roles, Role } from '../../common/decorators/roles.decorator';
import { CurrentTenant } from '../../common/decorators/tenant.decorator';
import { CurrentUser, JwtPayload } from '../../common/decorators/current-user.decorator';
import {
  AnalyticsAggregatorService,
} from './analytics-aggregator.service';
import {
  ReportGeneratorService,
  ReportType,
  ReportFormat,
  ReportRequest,
} from './report-generator.service';

// ============================================================================
// DTOs
// ============================================================================

class DateRangeQuery {
  from?: string;
  to?: string;
  granularity?: 'hourly' | 'daily' | 'weekly' | 'monthly';
}

class GenerateReportBody {
  type: ReportType;
  format: ReportFormat;
  dateFrom: string;
  dateTo: string;
  filters?: Record<string, unknown>;
  emailTo?: string;
}

// ============================================================================
// Controller
// ============================================================================

@ApiTags('analytics')
@ApiBearerAuth()
@UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
@Controller('analytics')
export class AdvancedAnalyticsController {
  constructor(
    private readonly aggregator: AnalyticsAggregatorService,
    private readonly reportGenerator: ReportGeneratorService,
  ) {}

  // --------------------------------------------------------------------------
  // Dashboard Overview
  // --------------------------------------------------------------------------

  @Get('overview')
  @ApiOperation({ summary: 'Get dashboard overview with KPIs' })
  async getOverview(
    @CurrentTenant() tenantId: string,
    @Query() query: DateRangeQuery,
  ) {
    const { from, to } = this.parseDateRange(query);

    const [metrics, agentScores, realtime] = await Promise.all([
      this.aggregator.aggregateRange(tenantId, from, to, query.granularity || 'daily'),
      this.aggregator.getAgentPerformanceScores(tenantId, from, to),
      this.aggregator.getRealtimeSnapshot(tenantId),
    ]);

    return {
      period: { from: from.toISOString(), to: to.toISOString() },
      kpis: {
        totalCalls: metrics.callVolume.total,
        successRate: metrics.successRates.overall,
        avgDuration: metrics.duration.average,
        avgSentiment: metrics.sentiment.averageScore,
        totalCost: metrics.cost.total,
        costPerCall: metrics.cost.perCall,
      },
      realtime,
      topAgents: agentScores.slice(0, 5),
    };
  }

  // --------------------------------------------------------------------------
  // Call Trends
  // --------------------------------------------------------------------------

  @Get('calls/trends')
  @ApiOperation({ summary: 'Get call volume trends with breakdowns' })
  @ApiQuery({ name: 'from', required: false })
  @ApiQuery({ name: 'to', required: false })
  @ApiQuery({ name: 'granularity', required: false, enum: ['hourly', 'daily', 'weekly', 'monthly'] })
  async getCallTrends(
    @CurrentTenant() tenantId: string,
    @Query() query: DateRangeQuery,
  ) {
    const { from, to } = this.parseDateRange(query);
    const metrics = await this.aggregator.aggregateRange(
      tenantId,
      from,
      to,
      query.granularity || 'daily',
    );

    return {
      period: { from: from.toISOString(), to: to.toISOString() },
      volume: metrics.callVolume,
      duration: metrics.duration,
      successRates: metrics.successRates,
    };
  }

  // --------------------------------------------------------------------------
  // Agent Performance
  // --------------------------------------------------------------------------

  @Get('agents/performance')
  @ApiOperation({ summary: 'Get agent performance leaderboard' })
  async getAgentPerformance(
    @CurrentTenant() tenantId: string,
    @Query() query: DateRangeQuery,
  ) {
    const { from, to } = this.parseDateRange(query);
    const scores = await this.aggregator.getAgentPerformanceScores(tenantId, from, to);

    return {
      period: { from: from.toISOString(), to: to.toISOString() },
      agents: scores,
      summary: {
        totalAgents: scores.length,
        avgScore: scores.length > 0
          ? scores.reduce((s, a) => s + a.score, 0) / scores.length
          : 0,
        topPerformer: scores[0] || null,
      },
    };
  }

  // --------------------------------------------------------------------------
  // Campaign Funnel
  // --------------------------------------------------------------------------

  @Get('campaigns/funnel')
  @ApiOperation({ summary: 'Get campaign funnel analytics' })
  @ApiQuery({ name: 'campaignId', required: false })
  async getCampaignFunnel(
    @CurrentTenant() tenantId: string,
    @Query('campaignId') campaignId?: string,
  ) {
    if (campaignId) {
      const funnel = await this.aggregator.getCampaignFunnel(tenantId, campaignId);
      return { funnels: [funnel] };
    }

    // Get funnels for all active campaigns
    const campaigns = await this.aggregator['prisma'].campaign.findMany({
      where: { tenantId, status: { in: ['RUNNING', 'COMPLETED'] } },
      select: { id: true },
      take: 20,
      orderBy: { createdAt: 'desc' },
    });

    const funnels = await Promise.all(
      campaigns.map((c) => this.aggregator.getCampaignFunnel(tenantId, c.id)),
    );

    return { funnels };
  }

  // --------------------------------------------------------------------------
  // Sentiment Trends
  // --------------------------------------------------------------------------

  @Get('sentiment/trends')
  @ApiOperation({ summary: 'Get sentiment analysis over time' })
  async getSentimentTrends(
    @CurrentTenant() tenantId: string,
    @Query() query: DateRangeQuery,
  ) {
    const { from, to } = this.parseDateRange(query);
    const metrics = await this.aggregator.aggregateRange(
      tenantId,
      from,
      to,
      query.granularity || 'daily',
    );

    return {
      period: { from: from.toISOString(), to: to.toISOString() },
      sentiment: metrics.sentiment,
    };
  }

  // --------------------------------------------------------------------------
  // Cost Breakdown
  // --------------------------------------------------------------------------

  @Get('costs/breakdown')
  @ApiOperation({ summary: 'Get cost analysis by dimension' })
  async getCostBreakdown(
    @CurrentTenant() tenantId: string,
    @Query() query: DateRangeQuery,
  ) {
    const { from, to } = this.parseDateRange(query);
    const metrics = await this.aggregator.aggregateRange(
      tenantId,
      from,
      to,
      query.granularity || 'daily',
    );

    return {
      period: { from: from.toISOString(), to: to.toISOString() },
      cost: metrics.cost,
    };
  }

  // --------------------------------------------------------------------------
  // Cohort Analysis
  // --------------------------------------------------------------------------

  @Get('cohorts')
  @ApiOperation({ summary: 'Get cohort analysis by contact segment' })
  async getCohortAnalysis(
    @CurrentTenant() tenantId: string,
    @Query() query: DateRangeQuery,
  ) {
    const { from, to } = this.parseDateRange(query);
    const cohorts = await this.aggregator.getCohortAnalysis(tenantId, from, to);

    return {
      period: { from: from.toISOString(), to: to.toISOString() },
      cohorts,
    };
  }

  // --------------------------------------------------------------------------
  // Real-time Metrics
  // --------------------------------------------------------------------------

  @Get('realtime')
  @ApiOperation({ summary: 'Get real-time metrics snapshot' })
  async getRealtimeMetrics(@CurrentTenant() tenantId: string) {
    return this.aggregator.getRealtimeSnapshot(tenantId);
  }

  // --------------------------------------------------------------------------
  // Report Generation
  // --------------------------------------------------------------------------

  @Post('reports/generate')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Generate a report asynchronously' })
  async generateReport(
    @CurrentTenant() tenantId: string,
    @CurrentUser() user: JwtPayload,
    @Body() body: GenerateReportBody,
  ) {
    const request: ReportRequest = {
      type: body.type,
      format: body.format,
      dateFrom: body.dateFrom,
      dateTo: body.dateTo,
      filters: body.filters,
      emailTo: body.emailTo,
    };

    const report = await this.reportGenerator.requestReport(
      tenantId,
      user.sub,
      request,
    );

    return {
      reportId: report.id,
      status: report.status,
      message: 'Report generation has been queued.',
    };
  }

  @Get('reports/:id')
  @ApiOperation({ summary: 'Get report status or download' })
  async getReport(
    @CurrentTenant() tenantId: string,
    @Param('id') reportId: string,
    @Query('download') download?: string,
  ) {
    if (download === 'true') {
      const url = await this.reportGenerator.getDownloadUrl(reportId, tenantId);
      return { downloadUrl: url };
    }

    const report = await this.reportGenerator.getReport(reportId);
    if (!report || report.tenantId !== tenantId) {
      return { error: 'Report not found' };
    }

    return report;
  }

  @Get('reports')
  @ApiOperation({ summary: 'List reports for tenant' })
  async listReports(@CurrentTenant() tenantId: string) {
    const reports = await this.reportGenerator.getReportsByTenant(tenantId);
    return { reports };
  }

  // --------------------------------------------------------------------------
  // Helpers
  // --------------------------------------------------------------------------

  private parseDateRange(query: DateRangeQuery): { from: Date; to: Date } {
    const to = query.to ? new Date(query.to) : new Date();
    const from = query.from
      ? new Date(query.from)
      : new Date(to.getTime() - 30 * 24 * 60 * 60 * 1000); // Default 30 days
    return { from, to };
  }
}
