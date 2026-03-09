import { Injectable, Logger } from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';
import { PaginationQuery, PaginatedResponse } from '../../common/dto/pagination.dto';
import { Prisma } from '@prisma/client';

export interface AuditFilterDto {
  userId?: string;
  action?: string;
  resource?: string;
  dateFrom?: string;
  dateTo?: string;
}

@Injectable()
export class AuditService {
  private readonly logger = new Logger(AuditService.name);

  constructor(private readonly prisma: PrismaService) {}

  async findAll(
    tenantId: string,
    query: PaginationQuery,
    filters?: AuditFilterDto,
  ): Promise<PaginatedResponse<any>> {
    const { page, limit, sortBy, sortOrder } = query;
    const skip = (page - 1) * limit;

    const where: Prisma.AuditLogWhereInput = { tenantId };

    if (filters?.userId) where.userId = filters.userId;
    if (filters?.action) where.action = { contains: filters.action, mode: 'insensitive' };
    if (filters?.resource) where.resource = { contains: filters.resource, mode: 'insensitive' };
    if (filters?.dateFrom) where.createdAt = { gte: new Date(filters.dateFrom) };
    if (filters?.dateTo) {
      where.createdAt = {
        ...(where.createdAt as Record<string, unknown>),
        lte: new Date(filters.dateTo),
      };
    }

    const [logs, total] = await Promise.all([
      this.prisma.auditLog.findMany({
        where,
        skip,
        take: limit,
        orderBy: { [sortBy || 'createdAt']: sortOrder || 'desc' },
      }),
      this.prisma.auditLog.count({ where }),
    ]);

    return new PaginatedResponse(logs, total, query);
  }

  async create(data: {
    userId: string | null;
    tenantId: string | null;
    action: string;
    resource: string;
    resourceId: string | null;
    details: Record<string, unknown>;
    ipAddress: string;
    userAgent: string | null;
  }): Promise<any> {
    return this.prisma.auditLog.create({ data });
  }
}
