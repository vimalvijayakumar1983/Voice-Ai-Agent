import {
  Controller,
  Get,
  Post,
  Put,
  Body,
  Query,
  Req,
  Res,
  UseGuards,
  HttpCode,
  HttpStatus,
  Logger,
  RawBodyRequest,
} from '@nestjs/common';
import { ApiTags, ApiOperation, ApiBearerAuth } from '@nestjs/swagger';
import { Request, Response } from 'express';
import { JwtAuthGuard } from '../../../common/guards/jwt-auth.guard';
import { RolesGuard } from '../../../common/guards/roles.guard';
import { TenantGuard } from '../../../common/guards/tenant.guard';
import { Roles, Role } from '../../../common/decorators/roles.decorator';
import { CurrentTenant } from '../../../common/decorators/tenant.decorator';
import { CRMSyncService } from './crm-sync.service';
import {
  CRMProvider,
  FieldMappingConfig,
  SyncOptions,
} from './crm.interface';

// ---------- DTOs ----------

interface ConnectCRMDto {
  provider: CRMProvider;
}

interface TriggerSyncDto {
  fullSync?: boolean;
  entityTypes?: string[];
  batchSize?: number;
}

interface UpdateMappingsDto {
  mappings: FieldMappingConfig[];
}

// ---------- Controller ----------

@ApiTags('integrations/crm')
@Controller('integrations/crm')
export class CRMSyncController {
  private readonly logger = new Logger(CRMSyncController.name);

  constructor(private readonly crmSyncService: CRMSyncService) {}

  @Post('connect')
  @UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
  @ApiBearerAuth()
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @ApiOperation({ summary: 'Initiate CRM OAuth connection flow' })
  async connect(
    @CurrentTenant() tenantId: string,
    @Body() dto: ConnectCRMDto,
  ) {
    const result = await this.crmSyncService.initiateOAuthFlow(tenantId, dto.provider);

    return {
      authorizationUrl: result.authorizationUrl,
      state: result.state,
    };
  }

  @Get('callback')
  @ApiOperation({ summary: 'Handle CRM OAuth callback' })
  async callback(
    @Query('code') code: string,
    @Query('state') state: string,
    @Res() res: Response,
  ) {
    try {
      const result = await this.crmSyncService.handleOAuthCallback(code, state);

      // Redirect to frontend integrations page with success
      const redirectUrl = `${process.env.FRONTEND_URL || 'http://localhost:3000'}/settings/integrations?connected=${result.provider}&integrationId=${result.integrationId}`;
      return res.redirect(redirectUrl);
    } catch (error) {
      this.logger.error(`CRM OAuth callback failed: ${(error as Error).message}`);

      const redirectUrl = `${process.env.FRONTEND_URL || 'http://localhost:3000'}/settings/integrations?error=${encodeURIComponent((error as Error).message)}`;
      return res.redirect(redirectUrl);
    }
  }

  @Post('sync')
  @UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
  @ApiBearerAuth()
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER)
  @HttpCode(HttpStatus.OK)
  @ApiOperation({ summary: 'Trigger manual CRM sync' })
  async triggerSync(
    @CurrentTenant() tenantId: string,
    @Body() dto: TriggerSyncDto,
  ) {
    const options: SyncOptions = {
      fullSync: dto.fullSync,
      entityTypes: dto.entityTypes,
      batchSize: dto.batchSize,
    };

    return this.crmSyncService.triggerSync(tenantId, options);
  }

  @Get('status')
  @UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
  @ApiBearerAuth()
  @ApiOperation({ summary: 'Get CRM sync status' })
  async getStatus(@CurrentTenant() tenantId: string) {
    return this.crmSyncService.getSyncStatus(tenantId);
  }

  @Put('mappings')
  @UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
  @ApiBearerAuth()
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @ApiOperation({ summary: 'Update CRM field mappings' })
  async updateMappings(
    @CurrentTenant() tenantId: string,
    @Body() dto: UpdateMappingsDto,
  ) {
    return this.crmSyncService.updateFieldMappings(tenantId, dto.mappings);
  }

  @Get('mappings')
  @UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
  @ApiBearerAuth()
  @ApiOperation({ summary: 'Get CRM field mappings' })
  async getMappings(@CurrentTenant() tenantId: string) {
    return this.crmSyncService.getFieldMappings(tenantId);
  }

  @Post('webhooks')
  @HttpCode(HttpStatus.OK)
  @ApiOperation({ summary: 'Handle CRM webhook events' })
  async handleWebhook(
    @Query('provider') provider: CRMProvider,
    @Query('tenantId') tenantId: string,
    @Body() payload: Record<string, unknown>,
  ) {
    if (!provider || !tenantId) {
      return { received: false, error: 'Missing provider or tenantId' };
    }

    await this.crmSyncService.handleWebhook(tenantId, provider, payload);

    return { received: true };
  }

  @Post('disconnect')
  @UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
  @ApiBearerAuth()
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN)
  @HttpCode(HttpStatus.OK)
  @ApiOperation({ summary: 'Disconnect CRM integration' })
  async disconnect(@CurrentTenant() tenantId: string) {
    await this.crmSyncService.disconnect(tenantId);
    return { disconnected: true };
  }
}
