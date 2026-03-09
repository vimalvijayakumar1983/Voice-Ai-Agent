import { Controller, Get, Query, UseGuards } from '@nestjs/common';
import { ApiTags, ApiOperation, ApiBearerAuth } from '@nestjs/swagger';
import { AnalyticsService } from './analytics.service';
import { JwtAuthGuard } from '../../common/guards/jwt-auth.guard';
import { RolesGuard } from '../../common/guards/roles.guard';
import { TenantGuard } from '../../common/guards/tenant.guard';
import { Roles, Role } from '../../common/decorators/roles.decorator';
import { CurrentTenant } from '../../common/decorators/tenant.decorator';

@ApiTags('analytics')
@ApiBearerAuth()
@UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
@Controller('analytics')
export class AnalyticsController {
  constructor(private readonly analyticsService: AnalyticsService) {}

  @Get('dashboard')
  @ApiOperation({ summary: 'Get dashboard statistics' })
  async getDashboard(@CurrentTenant() tenantId: string) {
    return this.analyticsService.getDashboardStats(tenantId);
  }

  @Get('calls')
  @ApiOperation({ summary: 'Get call metrics for date range' })
  async getCallMetrics(
    @CurrentTenant() tenantId: string,
    @Query('dateFrom') dateFrom: string,
    @Query('dateTo') dateTo: string,
  ) {
    return this.analyticsService.getCallMetrics(tenantId, dateFrom, dateTo);
  }

  @Get('campaigns')
  @ApiOperation({ summary: 'Get campaign metrics' })
  async getCampaignMetrics(@CurrentTenant() tenantId: string) {
    return this.analyticsService.getCampaignMetrics(tenantId);
  }

  @Get('agents')
  @ApiOperation({ summary: 'Get agent performance metrics' })
  async getAgentPerformance(@CurrentTenant() tenantId: string) {
    return this.analyticsService.getAgentPerformance(tenantId);
  }

  @Get('usage')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @ApiOperation({ summary: 'Get tenant usage statistics' })
  async getTenantUsage(@CurrentTenant() tenantId: string) {
    return this.analyticsService.getTenantUsage(tenantId);
  }
}
