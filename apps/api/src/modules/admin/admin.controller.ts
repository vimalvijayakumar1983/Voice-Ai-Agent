import {
  Controller,
  Get,
  Put,
  Body,
  Query,
  UseGuards,
} from '@nestjs/common';
import { ApiTags, ApiOperation, ApiBearerAuth } from '@nestjs/swagger';
import { AdminService } from './admin.service';
import { PaginationQuery } from '../../common/dto/pagination.dto';
import { JwtAuthGuard } from '../../common/guards/jwt-auth.guard';
import { RolesGuard } from '../../common/guards/roles.guard';
import { Roles, Role } from '../../common/decorators/roles.decorator';

@ApiTags('admin')
@ApiBearerAuth()
@UseGuards(JwtAuthGuard, RolesGuard)
@Roles(Role.SUPER_ADMIN)
@Controller('admin')
export class AdminController {
  constructor(private readonly adminService: AdminService) {}

  @Get('tenants')
  @ApiOperation({ summary: 'List all tenants (super admin)' })
  async listTenants(@Query() query: PaginationQuery) {
    return this.adminService.listTenants(query);
  }

  @Get('users')
  @ApiOperation({ summary: 'List all users across tenants' })
  async listUsers(@Query() query: PaginationQuery) {
    return this.adminService.listAllUsers(query);
  }

  @Get('feature-flags')
  @ApiOperation({ summary: 'Get feature flags' })
  async getFeatureFlags() {
    return this.adminService.getFeatureFlags();
  }

  @Put('feature-flags')
  @ApiOperation({ summary: 'Update a feature flag' })
  async updateFeatureFlag(
    @Body('key') key: string,
    @Body('value') value: boolean,
  ) {
    return this.adminService.updateFeatureFlag(key, value);
  }

  @Get('health')
  @ApiOperation({ summary: 'Get system health status' })
  async getHealth() {
    return this.adminService.getSystemHealth();
  }

  @Get('usage')
  @ApiOperation({ summary: 'Get platform usage overview' })
  async getUsage() {
    return this.adminService.getUsageOverview();
  }
}
