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
} from '@nestjs/common';
import { ApiTags, ApiOperation, ApiBearerAuth } from '@nestjs/swagger';
import { AgentsService } from './agents.service';
import { CreateAgentDto } from './dto/create-agent.dto';
import { UpdateAgentDto } from './dto/update-agent.dto';
import { PaginationQuery } from '../../common/dto/pagination.dto';
import { JwtAuthGuard } from '../../common/guards/jwt-auth.guard';
import { RolesGuard } from '../../common/guards/roles.guard';
import { TenantGuard } from '../../common/guards/tenant.guard';
import { Roles, Role } from '../../common/decorators/roles.decorator';
import { CurrentTenant } from '../../common/decorators/tenant.decorator';
import { CurrentUser, JwtPayload } from '../../common/decorators/current-user.decorator';

@ApiTags('agents')
@ApiBearerAuth()
@UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
@Controller('agents')
export class AgentsController {
  constructor(private readonly agentsService: AgentsService) {}

  @Get()
  @ApiOperation({ summary: 'List all agents for current tenant' })
  async findAll(
    @CurrentTenant() tenantId: string,
    @Query() query: PaginationQuery,
  ) {
    return this.agentsService.findAll(tenantId, query);
  }

  @Get(':id')
  @ApiOperation({ summary: 'Get agent by ID' })
  async findById(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
  ) {
    return this.agentsService.findById(tenantId, id);
  }

  @Post()
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Create a new agent' })
  async create(
    @CurrentTenant() tenantId: string,
    @CurrentUser() user: JwtPayload,
    @Body() dto: CreateAgentDto,
  ) {
    return this.agentsService.create(tenantId, user.sub, dto);
  }

  @Put(':id')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Update agent' })
  async update(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
    @Body() dto: UpdateAgentDto,
  ) {
    return this.agentsService.update(tenantId, id, dto);
  }

  @Delete(':id')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @ApiOperation({ summary: 'Deactivate agent' })
  async delete(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
  ) {
    return this.agentsService.delete(tenantId, id);
  }

  @Post(':id/versions')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Create a new version snapshot' })
  async createVersion(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
  ) {
    return this.agentsService.createVersion(tenantId, id);
  }

  @Get(':id/versions')
  @ApiOperation({ summary: 'List agent versions' })
  async getVersions(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
  ) {
    return this.agentsService.getVersions(tenantId, id);
  }

  @Post(':id/clone')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Clone an agent' })
  async clone(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
    @Body('name') name: string,
  ) {
    return this.agentsService.cloneAgent(tenantId, id, name);
  }

  @Post(':id/test')
  @ApiOperation({ summary: 'Test agent with sample input' })
  async test(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
    @Body('input') input: string,
  ) {
    return this.agentsService.testAgent(tenantId, id, input);
  }

  @Post(':id/demo-call')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Trigger a demo call to test agent voice and behavior' })
  async demoCall(
    @CurrentTenant() tenantId: string,
    @CurrentUser() user: JwtPayload,
    @Param('id') id: string,
    @Body() body: { toNumber: string; fromNumber?: string },
  ) {
    return this.agentsService.initiateDemoCall(tenantId, user.sub, id, body.toNumber, body.fromNumber);
  }
}
