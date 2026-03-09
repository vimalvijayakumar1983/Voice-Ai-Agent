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
import {
  KnowledgeBaseService,
  CreateKnowledgeBaseDto,
  UploadDocumentDto,
} from './knowledge-base.service';
import { PaginationQuery } from '../../common/dto/pagination.dto';
import { JwtAuthGuard } from '../../common/guards/jwt-auth.guard';
import { RolesGuard } from '../../common/guards/roles.guard';
import { TenantGuard } from '../../common/guards/tenant.guard';
import { Roles, Role } from '../../common/decorators/roles.decorator';
import { CurrentTenant } from '../../common/decorators/tenant.decorator';

@ApiTags('knowledge-base')
@ApiBearerAuth()
@UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
@Controller('knowledge-bases')
export class KnowledgeBaseController {
  constructor(private readonly kbService: KnowledgeBaseService) {}

  @Get()
  @ApiOperation({ summary: 'List knowledge bases' })
  async findAll(@CurrentTenant() tenantId: string, @Query() query: PaginationQuery) {
    return this.kbService.findAll(tenantId, query);
  }

  @Get(':id')
  @ApiOperation({ summary: 'Get knowledge base by ID' })
  async findById(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.kbService.findById(tenantId, id);
  }

  @Post()
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Create knowledge base' })
  async create(@CurrentTenant() tenantId: string, @Body() dto: CreateKnowledgeBaseDto) {
    return this.kbService.create(tenantId, dto);
  }

  @Put(':id')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Update knowledge base' })
  async update(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
    @Body() dto: Partial<CreateKnowledgeBaseDto>,
  ) {
    return this.kbService.update(tenantId, id, dto);
  }

  @Delete(':id')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @ApiOperation({ summary: 'Delete knowledge base' })
  async delete(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.kbService.delete(tenantId, id);
  }

  @Post(':id/documents')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Upload document to knowledge base' })
  async uploadDocument(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
    @Body() dto: UploadDocumentDto,
  ) {
    return this.kbService.uploadDocument(tenantId, id, dto);
  }

  @Get(':id/search')
  @ApiOperation({ summary: 'Search knowledge base' })
  async search(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
    @Query('q') queryText: string,
    @Query('topK') topK?: number,
  ) {
    return this.kbService.searchKnowledgeBase(tenantId, id, queryText, topK);
  }
}
