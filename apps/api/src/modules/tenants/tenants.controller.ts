import {
  Controller,
  Get,
  Post,
  Put,
  Patch,
  Delete,
  Body,
  Param,
  Query,
  UseGuards,
} from '@nestjs/common';
import { ApiTags, ApiOperation, ApiBearerAuth } from '@nestjs/swagger';
import { TenantsService } from './tenants.service';
import { CreateTenantDto, UpdateTenantDto, TenantSettingsDto } from './dto/create-tenant.dto';
import { PaginationQuery } from '../../common/dto/pagination.dto';
import { JwtAuthGuard } from '../../common/guards/jwt-auth.guard';
import { RolesGuard } from '../../common/guards/roles.guard';
import { TenantGuard } from '../../common/guards/tenant.guard';
import { Roles, Role } from '../../common/decorators/roles.decorator';
import { CurrentTenant } from '../../common/decorators/tenant.decorator';

@ApiTags('tenants')
@ApiBearerAuth()
@UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
@Controller('tenants')
export class TenantsController {
  constructor(private readonly tenantsService: TenantsService) {}

  @Get()
  @Roles(Role.SUPER_ADMIN)
  @ApiOperation({ summary: 'List all tenants (super admin only)' })
  async findAll(@Query() query: PaginationQuery) {
    return this.tenantsService.findAll(query);
  }

  @Get('current')
  @ApiOperation({ summary: 'Get current tenant details' })
  async getCurrent(@CurrentTenant() tenantId: string) {
    return this.tenantsService.findById(tenantId);
  }

  @Get(':id')
  @Roles(Role.SUPER_ADMIN, Role.TENANT_OWNER)
  @ApiOperation({ summary: 'Get tenant by ID' })
  async findById(@Param('id') id: string) {
    return this.tenantsService.findById(id);
  }

  @Post()
  @Roles(Role.SUPER_ADMIN)
  @ApiOperation({ summary: 'Create a new tenant' })
  async create(@Body() dto: CreateTenantDto) {
    return this.tenantsService.create(dto);
  }

  @Put(':id')
  @Roles(Role.SUPER_ADMIN, Role.TENANT_OWNER)
  @ApiOperation({ summary: 'Update tenant' })
  async update(@Param('id') id: string, @Body() dto: UpdateTenantDto) {
    return this.tenantsService.update(id, dto);
  }

  @Patch(':id/settings')
  @Roles(Role.SUPER_ADMIN, Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @ApiOperation({ summary: 'Update tenant settings' })
  async updateSettings(@Param('id') id: string, @Body() dto: TenantSettingsDto) {
    return this.tenantsService.updateSettings(id, dto);
  }

  @Delete(':id')
  @Roles(Role.SUPER_ADMIN)
  @ApiOperation({ summary: 'Deactivate tenant' })
  async delete(@Param('id') id: string) {
    return this.tenantsService.delete(id);
  }

  @Get(':id/members')
  @Roles(Role.SUPER_ADMIN, Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @ApiOperation({ summary: 'List tenant members' })
  async getMembers(@Param('id') id: string, @Query() query: PaginationQuery) {
    return this.tenantsService.getMembers(id, query);
  }
}
