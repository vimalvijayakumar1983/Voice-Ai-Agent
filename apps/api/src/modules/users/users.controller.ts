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
import { UsersService } from './users.service';
import { CreateUserDto, UpdateUserDto, InviteUserDto } from './dto/create-user.dto';
import { PaginationQuery } from '../../common/dto/pagination.dto';
import { JwtAuthGuard } from '../../common/guards/jwt-auth.guard';
import { RolesGuard } from '../../common/guards/roles.guard';
import { TenantGuard } from '../../common/guards/tenant.guard';
import { Roles, Role } from '../../common/decorators/roles.decorator';
import { CurrentTenant } from '../../common/decorators/tenant.decorator';

@ApiTags('users')
@ApiBearerAuth()
@UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
@Controller('users')
export class UsersController {
  constructor(private readonly usersService: UsersService) {}

  @Get()
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'List tenant users' })
  async findAll(
    @CurrentTenant() tenantId: string,
    @Query() query: PaginationQuery,
  ) {
    return this.usersService.findAll(tenantId, query);
  }

  @Get(':id')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Get user by ID' })
  async findById(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
  ) {
    return this.usersService.findById(tenantId, id);
  }

  @Post()
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @ApiOperation({ summary: 'Create a new user' })
  async create(
    @CurrentTenant() tenantId: string,
    @Body() dto: CreateUserDto,
  ) {
    return this.usersService.create(tenantId, dto);
  }

  @Put(':id')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @ApiOperation({ summary: 'Update user' })
  async update(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
    @Body() dto: UpdateUserDto,
  ) {
    return this.usersService.update(tenantId, id, dto);
  }

  @Patch(':id/role')
  @Roles(Role.TENANT_OWNER)
  @ApiOperation({ summary: 'Assign role to user' })
  async assignRole(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
    @Body('role') role: string,
  ) {
    return this.usersService.assignRole(tenantId, id, role);
  }

  @Post('invite')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @ApiOperation({ summary: 'Invite a new user to the tenant' })
  async invite(
    @CurrentTenant() tenantId: string,
    @Body() dto: InviteUserDto,
  ) {
    return this.usersService.inviteUser(tenantId, dto);
  }

  @Delete(':id')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @ApiOperation({ summary: 'Deactivate user' })
  async deactivate(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
  ) {
    return this.usersService.deactivate(tenantId, id);
  }
}
