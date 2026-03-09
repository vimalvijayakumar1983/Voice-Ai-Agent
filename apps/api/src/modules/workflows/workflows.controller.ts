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
import { WorkflowsService } from './workflows.service';
import { CreateWorkflowDto, UpdateWorkflowDto } from './dto/create-workflow.dto';
import { PaginationQuery } from '../../common/dto/pagination.dto';
import { JwtAuthGuard } from '../../common/guards/jwt-auth.guard';
import { RolesGuard } from '../../common/guards/roles.guard';
import { TenantGuard } from '../../common/guards/tenant.guard';
import { Roles, Role } from '../../common/decorators/roles.decorator';
import { CurrentTenant } from '../../common/decorators/tenant.decorator';
import { CurrentUser, JwtPayload } from '../../common/decorators/current-user.decorator';

@ApiTags('workflows')
@ApiBearerAuth()
@UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
@Controller('workflows')
export class WorkflowsController {
  constructor(private readonly workflowsService: WorkflowsService) {}

  @Get()
  @ApiOperation({ summary: 'List all workflows' })
  async findAll(@CurrentTenant() tenantId: string, @Query() query: PaginationQuery) {
    return this.workflowsService.findAll(tenantId, query);
  }

  @Get(':id')
  @ApiOperation({ summary: 'Get workflow by ID' })
  async findById(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.workflowsService.findById(tenantId, id);
  }

  @Post()
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Create a new workflow' })
  async create(
    @CurrentTenant() tenantId: string,
    @CurrentUser() user: JwtPayload,
    @Body() dto: CreateWorkflowDto,
  ) {
    return this.workflowsService.create(tenantId, user.sub, dto);
  }

  @Put(':id')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Update workflow' })
  async update(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
    @Body() dto: UpdateWorkflowDto,
  ) {
    return this.workflowsService.update(tenantId, id, dto);
  }

  @Delete(':id')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @ApiOperation({ summary: 'Deactivate workflow' })
  async delete(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.workflowsService.delete(tenantId, id);
  }

  @Post(':id/versions')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Create workflow version snapshot' })
  async createVersion(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.workflowsService.createVersion(tenantId, id);
  }

  @Get(':id/validate')
  @ApiOperation({ summary: 'Validate workflow structure' })
  async validate(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.workflowsService.validate(tenantId, id);
  }

  @Post(':id/clone')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Clone a workflow' })
  async clone(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
    @Body('name') name: string,
  ) {
    return this.workflowsService.clone(tenantId, id, name);
  }
}
