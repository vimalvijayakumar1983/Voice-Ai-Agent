import { Injectable, NotFoundException, Logger } from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';
import { S3Service } from '../../common/services/s3.service';
import { InitiateCallDto, CallFilterDto, CallStatus } from './dto/call.dto';
import { PaginationQuery, PaginatedResponse } from '../../common/dto/pagination.dto';
import { Prisma } from '@prisma/client';

@Injectable()
export class CallsService {
  private readonly logger = new Logger(CallsService.name);

  constructor(
    private readonly prisma: PrismaService,
    private readonly s3Service: S3Service,
  ) {}

  async findAll(
    tenantId: string,
    query: PaginationQuery,
    filters?: CallFilterDto,
  ): Promise<PaginatedResponse<any>> {
    const { page, limit, sortBy, sortOrder } = query;
    const skip = (page - 1) * limit;

    const where: Prisma.CallWhereInput = { tenantId };
    if (filters?.direction) where.direction = filters.direction;
    if (filters?.status) where.status = filters.status;
    if (filters?.agentId) where.agentId = filters.agentId;
    if (filters?.contactId) where.contactId = filters.contactId;
    if (filters?.campaignId) where.campaignId = filters.campaignId;
    if (filters?.dateFrom) where.startedAt = { gte: new Date(filters.dateFrom) };
    if (filters?.dateTo) {
      where.startedAt = {
        ...(where.startedAt as Record<string, unknown>),
        lte: new Date(filters.dateTo),
      };
    }

    const [calls, total] = await Promise.all([
      this.prisma.call.findMany({
        where,
        skip,
        take: limit,
        orderBy: { [sortBy || 'startedAt']: sortOrder || 'desc' },
        include: {
          agent: { select: { id: true, name: true } },
          contact: { select: { id: true, firstName: true, lastName: true, phone: true } },
        },
      }),
      this.prisma.call.count({ where }),
    ]);

    return new PaginatedResponse(calls, total, query);
  }

  async findById(tenantId: string, id: string): Promise<any> {
    const call = await this.prisma.call.findFirst({
      where: { id, tenantId },
      include: {
        agent: { select: { id: true, name: true, type: true } },
        contact: true,
      },
    });
    if (!call) throw new NotFoundException(`Call ${id} not found`);
    return call;
  }

  async getTranscript(tenantId: string, callId: string): Promise<any> {
    const call = await this.findById(tenantId, callId);

    const transcript = await this.prisma.callTranscript.findFirst({
      where: { callId },
      orderBy: { createdAt: 'desc' },
    });

    if (!transcript) throw new NotFoundException(`Transcript not found for call ${callId}`);
    return transcript;
  }

  async getRecordingUrl(tenantId: string, callId: string): Promise<{ url: string }> {
    const call = await this.findById(tenantId, callId);

    if (!call.recordingKey) {
      throw new NotFoundException(`No recording found for call ${callId}`);
    }

    const url = await this.s3Service.getSignedDownloadUrl(call.recordingKey, 3600);
    return { url };
  }

  async initiateCall(tenantId: string, userId: string, dto: InitiateCallDto): Promise<any> {
    const agent = await this.prisma.agent.findFirst({
      where: { id: dto.agentId, tenantId, isActive: true },
    });

    if (!agent) throw new NotFoundException(`Agent ${dto.agentId} not found or inactive`);

    const call = await this.prisma.call.create({
      data: {
        tenantId,
        agentId: dto.agentId,
        contactId: dto.contactId || null,
        campaignId: dto.campaignId || null,
        direction: 'OUTBOUND',
        status: CallStatus.QUEUED,
        toNumber: dto.toNumber,
        fromNumber: dto.fromNumber || null,
        initiatedById: userId,
        metadata: (dto.metadata || {}) as any,
      },
    });

    this.logger.log(`Outbound call ${call.id} initiated to ${dto.toNumber}`);

    // In production, trigger telephony provider here
    return call;
  }
}
