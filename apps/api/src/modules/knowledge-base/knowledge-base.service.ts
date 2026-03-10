import { Injectable, NotFoundException, Logger } from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';
import { S3Service } from '../../common/services/s3.service';
import { QueueService } from '../../common/services/queue.service';
import { PaginationQuery, PaginatedResponse } from '../../common/dto/pagination.dto';

export interface CreateKnowledgeBaseDto {
  name: string;
  description?: string;
  type?: string;
  metadata?: Record<string, unknown>;
}

export interface UploadDocumentDto {
  fileName: string;
  contentType: string;
  fileKey: string;
  metadata?: Record<string, unknown>;
}

@Injectable()
export class KnowledgeBaseService {
  private readonly logger = new Logger(KnowledgeBaseService.name);

  constructor(
    private readonly prisma: PrismaService,
    private readonly s3Service: S3Service,
    private readonly queueService: QueueService,
  ) {}

  async findAll(tenantId: string, query: PaginationQuery): Promise<PaginatedResponse<any>> {
    const { page, limit, sortBy, sortOrder } = query;
    const skip = (page - 1) * limit;
    const where = this.prisma.tenantWhere(tenantId);

    const [items, total] = await Promise.all([
      this.prisma.knowledgeBase.findMany({
        where,
        skip,
        take: limit,
        orderBy: { [sortBy || 'createdAt']: sortOrder || 'desc' },
        include: { _count: { select: { documents: true } } },
      }),
      this.prisma.knowledgeBase.count({ where }),
    ]);

    return new PaginatedResponse(items, total, query);
  }

  async findById(tenantId: string, id: string): Promise<any> {
    const kb = await this.prisma.knowledgeBase.findFirst({
      where: { id, tenantId },
      include: { documents: true },
    });
    if (!kb) throw new NotFoundException(`Knowledge base ${id} not found`);
    return kb;
  }

  async create(tenantId: string, dto: CreateKnowledgeBaseDto): Promise<any> {
    return this.prisma.knowledgeBase.create({
      data: {
        name: dto.name,
        description: dto.description,
        type: dto.type || 'general',
        tenantId,
        metadata: (dto.metadata || {}) as any,
      },
    });
  }

  async update(tenantId: string, id: string, dto: Partial<CreateKnowledgeBaseDto>): Promise<any> {
    await this.findById(tenantId, id);
    const updateData: Record<string, unknown> = {};
    if (dto.name !== undefined) updateData.name = dto.name;
    if (dto.description !== undefined) updateData.description = dto.description;
    if (dto.type !== undefined) updateData.type = dto.type;
    if (dto.metadata) updateData.metadata = dto.metadata as any;
    return this.prisma.knowledgeBase.update({
      where: { id },
      data: updateData,
    });
  }

  async delete(tenantId: string, id: string): Promise<void> {
    await this.findById(tenantId, id);
    await this.prisma.knowledgeBase.delete({ where: { id } });
  }

  async uploadDocument(
    tenantId: string,
    kbId: string,
    dto: UploadDocumentDto,
  ): Promise<any> {
    await this.findById(tenantId, kbId);

    const document = await this.prisma.knowledgeDocument.create({
      data: {
        knowledgeBaseId: kbId,
        name: dto.fileName,
        type: dto.contentType,
        storageKey: dto.fileKey,
        status: 'PENDING',
        metadata: (dto.metadata || {}) as any,
      },
    });

    // Queue document for processing (chunking, embedding, indexing)
    await this.queueService.addJob('kb-processing', 'process-document', {
      tenantId,
      documentId: document.id,
      knowledgeBaseId: kbId,
      fileKey: dto.fileKey,
    });

    this.logger.log(`Document ${document.id} queued for processing`);
    return document;
  }

  async searchKnowledgeBase(
    tenantId: string,
    kbId: string,
    queryText: string,
    topK = 5,
  ): Promise<any> {
    await this.findById(tenantId, kbId);

    // In production, this would perform vector similarity search
    // For now, do a basic text search
    const results = await this.prisma.documentChunk.findMany({
      where: {
        knowledgeBaseId: kbId,
        content: { contains: queryText, mode: 'insensitive' },
      },
      take: topK,
      orderBy: { createdAt: 'desc' },
    });

    return {
      query: queryText,
      results,
      count: results.length,
    };
  }
}
