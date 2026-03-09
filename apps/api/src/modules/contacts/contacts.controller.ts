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
import { ContactsService } from './contacts.service';
import {
  CreateContactDto,
  UpdateContactDto,
  BulkImportDto,
  ContactFilterDto,
} from './dto/create-contact.dto';
import { PaginationQuery } from '../../common/dto/pagination.dto';
import { JwtAuthGuard } from '../../common/guards/jwt-auth.guard';
import { RolesGuard } from '../../common/guards/roles.guard';
import { TenantGuard } from '../../common/guards/tenant.guard';
import { Roles, Role } from '../../common/decorators/roles.decorator';
import { CurrentTenant } from '../../common/decorators/tenant.decorator';

@ApiTags('contacts')
@ApiBearerAuth()
@UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
@Controller('contacts')
export class ContactsController {
  constructor(private readonly contactsService: ContactsService) {}

  @Get()
  @ApiOperation({ summary: 'List contacts with filters' })
  async findAll(
    @CurrentTenant() tenantId: string,
    @Query() query: PaginationQuery,
    @Query() filters: ContactFilterDto,
  ) {
    return this.contactsService.findAll(tenantId, query, filters);
  }

  @Get(':id')
  @ApiOperation({ summary: 'Get contact by ID' })
  async findById(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.contactsService.findById(tenantId, id);
  }

  @Post()
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER, Role.AGENT)
  @ApiOperation({ summary: 'Create a new contact' })
  async create(@CurrentTenant() tenantId: string, @Body() dto: CreateContactDto) {
    return this.contactsService.create(tenantId, dto);
  }

  @Put(':id')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER, Role.AGENT)
  @ApiOperation({ summary: 'Update contact' })
  async update(
    @CurrentTenant() tenantId: string,
    @Param('id') id: string,
    @Body() dto: UpdateContactDto,
  ) {
    return this.contactsService.update(tenantId, id, dto);
  }

  @Delete(':id')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Delete contact' })
  async delete(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.contactsService.delete(tenantId, id);
  }

  @Post('bulk-import')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Bulk import contacts from CSV' })
  async bulkImport(@CurrentTenant() tenantId: string, @Body() dto: BulkImportDto) {
    return this.contactsService.bulkImport(tenantId, dto);
  }

  @Post('export')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @ApiOperation({ summary: 'Export contacts to CSV' })
  async export(@CurrentTenant() tenantId: string, @Body() filters: ContactFilterDto) {
    return this.contactsService.export(tenantId, filters);
  }
}
