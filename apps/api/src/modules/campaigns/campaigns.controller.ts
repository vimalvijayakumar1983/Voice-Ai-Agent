import {
  Controller,
  Get,
  Post,
  Put,
  Body,
  Param,
  Query,
  UseGuards,
  HttpCode,
  HttpStatus,
} from '@nestjs/common';
import { ApiTags, ApiOperation, ApiBearerAuth } from '@nestjs/swagger';
import { CampaignsService } from './campaigns.service';
import { CreateCampaignDto, UpdateCampaignDto, CampaignFilterDto } from './dto/campaign.dto';
import { PaginationQuery } from '../../common/dto/pagination.dto';
import { JwtAuthGuard } from '../../common/guards/jwt-auth.guard';
import { RolesGuard } from '../../common/guards/roles.guard';
import { TenantGuard } from '../../common/guards/tenant.guard';
import { Roles, Role } from '../../common/decorators/roles.decorator';
import { CurrentTenant } from '../../common/decorators/tenant.decorator';
import { CurrentUser, JwtPayload } from '../../common/decorators/current-user.decorator';

@ApiTags('campaigns')
@ApiBearerAuth()
@UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
@Controller('campaigns')
export class CampaignsController {
  constructor(private readonly campaignsService: CampaignsService) {}

  @Get()
  @ApiOperation({ summary: 'List campaigns' })
  async findAll(
    @CurrentTenant() tenantId: string,
    @Query() query: PaginationQuery,
    @Query() filters: CampaignFilterDto,
  ) {
    return this.campaignsService.findAll(tenantId, query, filters);
  }

  @Get(':id')
  @ApiOperation({ summary: 'Get campaign by ID' })
  async findById(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.campaignsService.findById(tenantId, id);
  }

  @Post()
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Create a campaign' })
  async create(
    @CurrentTenant() tenantId: string,
    @CurrentUser() user: JwtPayload,
    @Body() dto: CreateCampaignDto,
  ) {
    return this.campaignsService.create(tenantId, user.sub, dto);
  }

  @Put(':id')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Update campaign' })
  async update(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
    @Body() dto: UpdateCampaignDto,
  ) {
    return this.campaignsService.update(tenantId, id, dto);
  }

  @Post(':id/start')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @HttpCode(HttpStatus.OK)
  @ApiOperation({ summary: 'Start campaign' })
  async start(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.campaignsService.start(tenantId, id);
  }

  @Post(':id/pause')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @HttpCode(HttpStatus.OK)
  @ApiOperation({ summary: 'Pause campaign' })
  async pause(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.campaignsService.pause(tenantId, id);
  }

  @Post(':id/resume')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @HttpCode(HttpStatus.OK)
  @ApiOperation({ summary: 'Resume campaign' })
  async resume(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.campaignsService.resume(tenantId, id);
  }

  @Post(':id/cancel')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @HttpCode(HttpStatus.OK)
  @ApiOperation({ summary: 'Cancel campaign' })
  async cancel(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.campaignsService.cancel(tenantId, id);
  }

  @Get(':id/stats')
  @ApiOperation({ summary: 'Get campaign statistics' })
  async getStats(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.campaignsService.getStats(tenantId, id);
  }
}
