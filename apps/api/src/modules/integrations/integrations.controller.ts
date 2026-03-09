import {
  Controller,
  Get,
  Post,
  Put,
  Delete,
  Body,
  Param,
  Query,
  UseGuards,
  HttpCode,
  HttpStatus,
} from '@nestjs/common';
import { ApiTags, ApiOperation, ApiBearerAuth } from '@nestjs/swagger';
import { IntegrationsService, CreateIntegrationDto, UpdateIntegrationDto } from './integrations.service';
import { PaginationQuery } from '../../common/dto/pagination.dto';
import { JwtAuthGuard } from '../../common/guards/jwt-auth.guard';
import { RolesGuard } from '../../common/guards/roles.guard';
import { TenantGuard } from '../../common/guards/tenant.guard';
import { Roles, Role } from '../../common/decorators/roles.decorator';
import { CurrentTenant } from '../../common/decorators/tenant.decorator';

@ApiTags('integrations')
@ApiBearerAuth()
@UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
@Controller('integrations')
export class IntegrationsController {
  constructor(private readonly integrationsService: IntegrationsService) {}

  @Get()
  @ApiOperation({ summary: 'List integrations' })
  async findAll(@CurrentTenant() tenantId: string, @Query() query: PaginationQuery) {
    return this.integrationsService.findAll(tenantId, query);
  }

  @Get(':id')
  @ApiOperation({ summary: 'Get integration by ID' })
  async findById(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.integrationsService.findById(tenantId, id);
  }

  @Post()
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @ApiOperation({ summary: 'Create integration' })
  async create(@CurrentTenant() tenantId: string, @Body() dto: CreateIntegrationDto) {
    return this.integrationsService.create(tenantId, dto);
  }

  @Put(':id')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @ApiOperation({ summary: 'Update integration' })
  async update(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
    @Body() dto: UpdateIntegrationDto,
  ) {
    return this.integrationsService.update(tenantId, id, dto);
  }

  @Delete(':id')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @ApiOperation({ summary: 'Delete integration' })
  async delete(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.integrationsService.delete(tenantId, id);
  }

  @Post(':id/test')
  @HttpCode(HttpStatus.OK)
  @ApiOperation({ summary: 'Test integration connection' })
  async testConnection(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.integrationsService.testConnection(tenantId, id);
  }

  @Post(':id/sync')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @HttpCode(HttpStatus.OK)
  @ApiOperation({ summary: 'Trigger integration sync' })
  async sync(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.integrationsService.sync(tenantId, id);
  }
}
