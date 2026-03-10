import {
  Injectable,
  NotFoundException,
  BadRequestException,
  Logger,
} from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';
import { QueueService } from '../../common/services/queue.service';
import {
  CreateCampaignDto,
  UpdateCampaignDto,
  CampaignFilterDto,
  CampaignStatus,
} from './dto/campaign.dto';
import { PaginationQuery, PaginatedResponse } from '../../common/dto/pagination.dto';
import { Prisma } from '@prisma/client';

@Injectable()
export class CampaignsService {
  private readonly logger = new Logger(CampaignsService.name);

  constructor(
    private readonly prisma: PrismaService,
    private readonly queueService: QueueService,
  ) {}

  async findAll(
    tenantId: string,
    query: PaginationQuery,
    filters?: CampaignFilterDto,
  ): Promise<PaginatedResponse<any>> {
    const { page, limit, sortBy, sortOrder } = query;
    const skip = (page - 1) * limit;

    const where: Prisma.CampaignWhereInput = { tenantId };
    if (filters?.status) where.status = filters.status;
    if (filters?.type) where.type = filters.type;
    if (filters?.search) {
      where.OR = [
        { name: { contains: filters.search, mode: 'insensitive' } },
        { description: { contains: filters.search, mode: 'insensitive' } },
      ];
    }

    const [campaigns, total] = await Promise.all([
      this.prisma.campaign.findMany({
        where,
        skip,
        take: limit,
        orderBy: { [sortBy || 'createdAt']: sortOrder || 'desc' },
        include: {
          agent: { select: { id: true, name: true } },
          _count: { select: { calls: true } },
        },
      }),
      this.prisma.campaign.count({ where }),
    ]);

    return new PaginatedResponse(campaigns, total, query);
  }

  async findById(tenantId: string, id: string): Promise<any> {
    const campaign = await this.prisma.campaign.findFirst({
      where: { id, tenantId },
      include: {
        agent: { select: { id: true, name: true, type: true } },
        _count: { select: { calls: true } },
      },
    });
    if (!campaign) throw new NotFoundException(`Campaign ${id} not found`);
    return campaign;
  }

  async create(tenantId: string, userId: string, dto: CreateCampaignDto): Promise<any> {
    const agent = await this.prisma.agent.findFirst({
      where: { id: dto.agentId, tenantId, isActive: true },
    });
    if (!agent) throw new NotFoundException(`Agent ${dto.agentId} not found or inactive`);

    return this.prisma.campaign.create({
      data: {
        name: dto.name,
        description: dto.description,
        type: dto.type,
        status: CampaignStatus.DRAFT,
        agentId: dto.agentId,
        tenantId,
        createdBy: userId,
        contactListIds: dto.contactListIds || [],
        scheduledStartAt: dto.scheduledStartAt ? new Date(dto.scheduledStartAt) : null,
        maxConcurrentCalls: dto.maxConcurrentCalls || 5,
        callsPerHour: dto.callsPerHour || 100,
        callingHours: dto.callingHours ? JSON.parse(JSON.stringify(dto.callingHours)) : {},
        maxRetries: dto.maxRetries || 2,
        retryDelayMinutes: dto.retryDelayMinutes || 60,
        metadata: (dto.metadata || {}) as any,
      },
    });
  }

  async update(tenantId: string, id: string, dto: UpdateCampaignDto): Promise<any> {
    const campaign = await this.findById(tenantId, id);

    if (['RUNNING', 'COMPLETED', 'CANCELLED'].includes(campaign.status)) {
      throw new BadRequestException(`Cannot update campaign in ${campaign.status} state`);
    }

    const updateData: Record<string, unknown> = { ...dto };
    if (dto.callingHours) {
      updateData.callingHours = JSON.parse(JSON.stringify(dto.callingHours));
    }
    if (dto.scheduledStartAt) updateData.scheduledStartAt = new Date(dto.scheduledStartAt);

    return this.prisma.campaign.update({ where: { id }, data: updateData });
  }

  async start(tenantId: string, id: string): Promise<any> {
    const campaign = await this.findById(tenantId, id);

    if (!['DRAFT', 'SCHEDULED', 'PAUSED'].includes(campaign.status)) {
      throw new BadRequestException(`Cannot start campaign in ${campaign.status} state`);
    }

    await this.prisma.campaign.update({
      where: { id },
      data: { status: CampaignStatus.RUNNING, startedAt: new Date() },
    });

    await this.queueService.addJob('campaign-execution', 'start-campaign', {
      tenantId,
      campaignId: id,
    });

    this.logger.log(`Campaign ${id} started`);
    return this.findById(tenantId, id);
  }

  async pause(tenantId: string, id: string): Promise<any> {
    const campaign = await this.findById(tenantId, id);
    if (campaign.status !== CampaignStatus.RUNNING) {
      throw new BadRequestException('Can only pause running campaigns');
    }

    return this.prisma.campaign.update({
      where: { id },
      data: { status: CampaignStatus.PAUSED },
    });
  }

  async resume(tenantId: string, id: string): Promise<any> {
    const campaign = await this.findById(tenantId, id);
    if (campaign.status !== CampaignStatus.PAUSED) {
      throw new BadRequestException('Can only resume paused campaigns');
    }

    await this.prisma.campaign.update({
      where: { id },
      data: { status: CampaignStatus.RUNNING },
    });

    await this.queueService.addJob('campaign-execution', 'resume-campaign', {
      tenantId,
      campaignId: id,
    });

    return this.findById(tenantId, id);
  }

  async cancel(tenantId: string, id: string): Promise<any> {
    const campaign = await this.findById(tenantId, id);
    if (['COMPLETED', 'CANCELLED'].includes(campaign.status)) {
      throw new BadRequestException(`Campaign already ${campaign.status.toLowerCase()}`);
    }

    return this.prisma.campaign.update({
      where: { id },
      data: { status: CampaignStatus.CANCELLED, completedAt: new Date() },
    });
  }

  async getStats(tenantId: string, id: string): Promise<any> {
    await this.findById(tenantId, id);

    const stats = await this.prisma.call.groupBy({
      by: ['status'],
      where: { campaignId: id, tenantId },
      _count: { id: true },
    });

    const totalCalls = stats.reduce((sum, s) => sum + s._count.id, 0);
    const completedCalls = stats.find((s) => s.status === 'COMPLETED')?._count.id || 0;

    const avgDuration = await this.prisma.call.aggregate({
      where: { campaignId: id, tenantId, status: 'COMPLETED' },
      _avg: { durationSeconds: true },
    });

    return {
      totalCalls,
      completedCalls,
      statusBreakdown: stats.reduce(
        (acc, s) => ({ ...acc, [s.status]: s._count.id }),
        {},
      ),
      averageDurationSeconds: avgDuration._avg.durationSeconds || 0,
      completionRate: totalCalls > 0 ? (completedCalls / totalCalls) * 100 : 0,
    };
  }
}
