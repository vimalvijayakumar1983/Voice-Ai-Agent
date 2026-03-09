import { Controller, Get, Param, Query, UseGuards } from '@nestjs/common';
import { ApiTags, ApiOperation, ApiBearerAuth } from '@nestjs/swagger';
import { BillingService } from './billing.service';
import { PaginationQuery } from '../../common/dto/pagination.dto';
import { JwtAuthGuard } from '../../common/guards/jwt-auth.guard';
import { RolesGuard } from '../../common/guards/roles.guard';
import { TenantGuard } from '../../common/guards/tenant.guard';
import { Roles, Role } from '../../common/decorators/roles.decorator';
import { CurrentTenant } from '../../common/decorators/tenant.decorator';

@ApiTags('billing')
@ApiBearerAuth()
@UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
@Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
@Controller('billing')
export class BillingController {
  constructor(private readonly billingService: BillingService) {}

  @Get('subscription')
  @ApiOperation({ summary: 'Get current subscription' })
  async getSubscription(@CurrentTenant() tenantId: string) {
    return this.billingService.getSubscription(tenantId);
  }

  @Get('usage')
  @ApiOperation({ summary: 'Get current usage stats' })
  async getUsage(@CurrentTenant() tenantId: string) {
    return this.billingService.getUsage(tenantId);
  }

  @Get('invoices')
  @ApiOperation({ summary: 'List invoices' })
  async getInvoices(@CurrentTenant() tenantId: string, @Query() query: PaginationQuery) {
    return this.billingService.getInvoices(tenantId, query);
  }

  @Get('invoices/:invoiceId')
  @ApiOperation({ summary: 'Get invoice details' })
  async getInvoice(
    @CurrentTenant() tenantId: string,
    @Param('invoiceId') invoiceId: string,
  ) {
    return this.billingService.getInvoiceById(tenantId, invoiceId);
  }
}
