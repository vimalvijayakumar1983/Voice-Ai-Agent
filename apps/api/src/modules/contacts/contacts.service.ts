import { Injectable, NotFoundException, Logger } from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';
import { QueueService } from '../../common/services/queue.service';
import {
  CreateContactDto,
  UpdateContactDto,
  BulkImportDto,
  ContactFilterDto,
} from './dto/create-contact.dto';
import { PaginationQuery, PaginatedResponse } from '../../common/dto/pagination.dto';
import { Prisma } from '@prisma/client';

@Injectable()
export class ContactsService {
  private readonly logger = new Logger(ContactsService.name);

  constructor(
    private readonly prisma: PrismaService,
    private readonly queueService: QueueService,
  ) {}

  async findAll(
    tenantId: string,
    query: PaginationQuery,
    filters?: ContactFilterDto,
  ): Promise<PaginatedResponse<any>> {
    const { page, limit, sortBy, sortOrder } = query;
    const skip = (page - 1) * limit;

    const where: Prisma.ContactWhereInput = { tenantId };

    if (filters?.search) {
      where.OR = [
        { firstName: { contains: filters.search, mode: 'insensitive' } },
        { lastName: { contains: filters.search, mode: 'insensitive' } },
        { email: { contains: filters.search, mode: 'insensitive' } },
        { phone: { contains: filters.search } },
        { company: { contains: filters.search, mode: 'insensitive' } },
      ];
    }

    if (filters?.tags?.length) {
      where.tags = { hasSome: filters.tags };
    }
    if (filters?.source) where.source = filters.source;
    if (filters?.company) where.company = { contains: filters.company, mode: 'insensitive' };
    if (filters?.createdAfter) where.createdAt = { gte: new Date(filters.createdAfter) };
    if (filters?.createdBefore) {
      where.createdAt = {
        ...(where.createdAt as Record<string, unknown>),
        lte: new Date(filters.createdBefore),
      };
    }

    const [contacts, total] = await Promise.all([
      this.prisma.contact.findMany({
        where,
        skip,
        take: limit,
        orderBy: { [sortBy || 'createdAt']: sortOrder || 'desc' },
      }),
      this.prisma.contact.count({ where }),
    ]);

    return new PaginatedResponse(contacts, total, query);
  }

  async findById(tenantId: string, id: string): Promise<any> {
    const contact = await this.prisma.contact.findFirst({
      where: { id, tenantId },
    });
    if (!contact) throw new NotFoundException(`Contact ${id} not found`);
    return contact;
  }

  async create(tenantId: string, dto: CreateContactDto): Promise<any> {
    // Dedup check by phone within tenant
    const existing = await this.prisma.contact.findFirst({
      where: { tenantId, phone: dto.phone },
    });

    if (existing) {
      this.logger.warn(`Duplicate contact phone ${dto.phone} in tenant ${tenantId}`);
      return existing;
    }

    const { jobTitle, notes, preferredLanguage, ...contactData } = dto as any;
    return this.prisma.contact.create({
      data: {
        ...contactData,
        tenantId,
        tags: dto.tags || [],
        customFields: (dto.customFields || {}) as any,
        metadata: {
          ...(jobTitle ? { jobTitle } : {}),
          ...(notes ? { notes } : {}),
          ...(preferredLanguage ? { preferredLanguage } : {}),
        },
      },
    });
  }

  async update(tenantId: string, id: string, dto: UpdateContactDto): Promise<any> {
    await this.findById(tenantId, id);

    const updateData: Record<string, unknown> = { ...dto };
    if (dto.customFields) {
      updateData.customFields = dto.customFields;
    }

    return this.prisma.contact.update({
      where: { id },
      data: updateData,
    });
  }

  async delete(tenantId: string, id: string): Promise<void> {
    await this.findById(tenantId, id);
    await this.prisma.contact.delete({ where: { id } });
  }

  async bulkImport(tenantId: string, dto: BulkImportDto): Promise<{ jobId: string }> {
    const job = await this.queueService.addJob('contacts-import', 'bulk-import', {
      tenantId,
      fileKey: dto.fileKey,
      columnMapping: dto.columnMapping || {},
      tags: dto.tags || [],
      skipDuplicates: dto.skipDuplicates ?? true,
    });

    this.logger.log(`Bulk import job ${job.id} queued for tenant ${tenantId}`);
    return { jobId: job.id! };
  }

  async export(tenantId: string, filters?: ContactFilterDto): Promise<{ fileKey: string }> {
    const job = await this.queueService.addJob('contacts-export', 'export', {
      tenantId,
      filters: filters || {},
    });

    return { fileKey: `exports/${tenantId}/contacts-${job.id}.csv` };
  }
}
