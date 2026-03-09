import {
  Controller,
  Get,
  Post,
  Patch,
  Delete,
  Body,
  Param,
  UseGuards,
} from '@nestjs/common';
import { ApiTags, ApiOperation, ApiBearerAuth } from '@nestjs/swagger';
import { JwtAuthGuard } from '../../common/guards/jwt-auth.guard';
import { RolesGuard } from '../../common/guards/roles.guard';
import { TenantGuard } from '../../common/guards/tenant.guard';
import { Roles, Role } from '../../common/decorators/roles.decorator';
import { CurrentTenant } from '../../common/decorators/tenant.decorator';
import { CurrentUser, JwtPayload } from '../../common/decorators/current-user.decorator';
import {
  AbTestingService,
  CreateExperimentDto,
  UpdateExperimentDto,
} from './ab-testing.service';

@ApiTags('ab-testing')
@ApiBearerAuth()
@UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
@Controller('ab-tests')
export class AbTestingController {
  constructor(private readonly abTestingService: AbTestingService) {}

  @Get()
  @ApiOperation({ summary: 'List all A/B test experiments' })
  async findAll(@CurrentTenant() tenantId: string) {
    const experiments = await this.abTestingService.findAll(tenantId);
    return { experiments };
  }

  @Post()
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Create a new A/B test experiment' })
  async create(
    @CurrentTenant() tenantId: string,
    @CurrentUser() user: JwtPayload,
    @Body() dto: CreateExperimentDto,
  ) {
    return this.abTestingService.create(tenantId, user.sub, dto);
  }

  @Get(':id')
  @ApiOperation({ summary: 'Get experiment details' })
  async findById(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
  ) {
    return this.abTestingService.findById(tenantId, id);
  }

  @Patch(':id')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Update experiment' })
  async update(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
    @Body() dto: UpdateExperimentDto,
  ) {
    return this.abTestingService.update(tenantId, id, dto);
  }

  @Delete(':id')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @ApiOperation({ summary: 'Delete experiment' })
  async delete(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
  ) {
    await this.abTestingService.delete(tenantId, id);
    return { message: 'Experiment deleted' };
  }

  @Post(':id/start')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Start an experiment' })
  async start(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
  ) {
    return this.abTestingService.startExperiment(tenantId, id);
  }

  @Post(':id/stop')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Stop an experiment' })
  async stop(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
  ) {
    return this.abTestingService.stopExperiment(tenantId, id);
  }

  @Post(':id/apply-winner')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @ApiOperation({ summary: 'Apply winning variant to agent' })
  async applyWinner(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
  ) {
    return this.abTestingService.applyWinner(tenantId, id);
  }

  @Get(':id/results')
  @ApiOperation({ summary: 'Get experiment results with statistics' })
  async getResults(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
  ) {
    return this.abTestingService.getResults(tenantId, id);
  }
}
