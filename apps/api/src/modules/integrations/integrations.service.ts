import { Injectable, NotFoundException, BadRequestException, Logger } from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';
import { RedisService } from '../../common/services/redis.service';
import { PaginationQuery, PaginatedResponse } from '../../common/dto/pagination.dto';

export interface CreateIntegrationDto {
  name: string;
  type: string; // 'CRM' | 'CALENDAR' | 'EMAIL' | 'WEBHOOK' | 'ZAPIER' | 'CUSTOM'
  provider: string; // 'salesforce' | 'hubspot' | 'google_calendar' | etc.
  config: Record<string, unknown>;
  credentials?: Record<string, unknown>;
  isActive?: boolean;
}

export interface UpdateIntegrationDto {
  name?: string;
  config?: Record<string, unknown>;
  credentials?: Record<string, unknown>;
  isActive?: boolean;
}

@Injectable()
export class IntegrationsService {
  private readonly logger = new Logger(IntegrationsService.name);

  constructor(
    private readonly prisma: PrismaService,
    private readonly redisService: RedisService,
  ) {}

  async findAll(tenantId: string, query: PaginationQuery): Promise<PaginatedResponse<any>> {
    const { page, limit, sortBy, sortOrder } = query;
    const skip = (page - 1) * limit;
    const where = this.prisma.tenantWhere(tenantId);

    const [integrations, total] = await Promise.all([
      this.prisma.integration.findMany({
        where,
        skip,
        take: limit,
        orderBy: { [sortBy || 'createdAt']: sortOrder || 'desc' },
        select: {
          id: true,
          name: true,
          type: true,
          provider: true,
          config: true,
          isActive: true,
          lastSyncAt: true,
          createdAt: true,
        },
      }),
      this.prisma.integration.count({ where }),
    ]);

    return new PaginatedResponse(integrations, total, query);
  }

  async findById(tenantId: string, id: string): Promise<any> {
    const integration = await this.prisma.integration.findFirst({
      where: { id, tenantId },
    });
    if (!integration) throw new NotFoundException(`Integration ${id} not found`);
    return integration;
  }

  async create(tenantId: string, dto: CreateIntegrationDto): Promise<any> {
    return this.prisma.integration.create({
      data: {
        name: dto.name,
        type: dto.type,
        provider: dto.provider,
        config: dto.config as any,
        credentials: dto.credentials ? JSON.parse(JSON.stringify(dto.credentials)) : {},
        tenantId,
        isActive: dto.isActive ?? true,
      },
    });
  }

  async update(tenantId: string, id: string, dto: UpdateIntegrationDto): Promise<any> {
    await this.findById(tenantId, id);

    const updateData: Record<string, unknown> = {};
    if (dto.name !== undefined) updateData.name = dto.name;
    if (dto.config) updateData.config = dto.config;
    if (dto.credentials) updateData.credentials = JSON.parse(JSON.stringify(dto.credentials));
    if (dto.isActive !== undefined) updateData.isActive = dto.isActive;

    return this.prisma.integration.update({ where: { id }, data: updateData });
  }

  async delete(tenantId: string, id: string): Promise<void> {
    await this.findById(tenantId, id);
    await this.prisma.integration.delete({ where: { id } });
  }

  async testConnection(tenantId: string, id: string): Promise<{ success: boolean; message: string }> {
    const integration = await this.findById(tenantId, id);

    // In production, actually test the connection based on provider
    try {
      this.logger.log(`Testing connection for integration ${id} (${integration.provider})`);
      // Simulate connection test
      return { success: true, message: `Connection to ${integration.provider} successful` };
    } catch (error) {
      return { success: false, message: `Connection failed: ${(error as Error).message}` };
    }
  }

  async sync(tenantId: string, id: string): Promise<{ jobId: string }> {
    const integration = await this.findById(tenantId, id);

    if (!integration.isActive) {
      throw new BadRequestException('Cannot sync inactive integration');
    }

    // In production, queue a sync job
    await this.prisma.integration.update({
      where: { id },
      data: { lastSyncAt: new Date() },
    });

    this.logger.log(`Sync triggered for integration ${id}`);
    return { jobId: `sync-${id}-${Date.now()}` };
  }
}
