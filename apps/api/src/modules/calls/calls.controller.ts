import {
  Controller,
  Get,
  Post,
  Body,
  Param,
  Query,
  UseGuards,
} from '@nestjs/common';
import { ApiTags, ApiOperation, ApiBearerAuth } from '@nestjs/swagger';
import { CallsService } from './calls.service';
import { InitiateCallDto, CallFilterDto } from './dto/call.dto';
import { PaginationQuery } from '../../common/dto/pagination.dto';
import { JwtAuthGuard } from '../../common/guards/jwt-auth.guard';
import { RolesGuard } from '../../common/guards/roles.guard';
import { TenantGuard } from '../../common/guards/tenant.guard';
import { Roles, Role } from '../../common/decorators/roles.decorator';
import { CurrentTenant } from '../../common/decorators/tenant.decorator';
import { CurrentUser, JwtPayload } from '../../common/decorators/current-user.decorator';

@ApiTags('calls')
@ApiBearerAuth()
@UseGuards(JwtAuthGuard, RolesGuard, TenantGuard)
@Controller('calls')
export class CallsController {
  constructor(private readonly callsService: CallsService) {}

  @Get()
  @ApiOperation({ summary: 'List calls with filters' })
  async findAll(
    @CurrentTenant() tenantId: string,
    @Query() query: PaginationQuery,
    @Query() filters: CallFilterDto,
  ) {
    return this.callsService.findAll(tenantId, query, filters);
  }

  @Get(':id')
  @ApiOperation({ summary: 'Get call details' })
  async findById(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.callsService.findById(tenantId, id);
  }

  @Get(':id/transcript')
  @ApiOperation({ summary: 'Get call transcript' })
  async getTranscript(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.callsService.getTranscript(tenantId, id);
  }

  @Get(':id/recording')
  @ApiOperation({ summary: 'Get signed URL for call recording' })
  async getRecording(@CurrentTenant() tenantId: string, @Param('id') id: string) {
    return this.callsService.getRecordingUrl(tenantId, id);
  }

  @Post('initiate')
  @Roles(Role.TENANT_OWNER, Role.TENANT_ADMIN, Role.MANAGER, Role.AGENT)
  @ApiOperation({ summary: 'Initiate an outbound call' })
  async initiateCall(
    @CurrentTenant() tenantId: string,
    @CurrentUser() user: JwtPayload,
    @Body() dto: InitiateCallDto,
  ) {
    return this.callsService.initiateCall(tenantId, user.sub, dto);
  }
}
