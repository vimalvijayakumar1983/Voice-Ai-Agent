import { Logger } from '@nestjs/common';
import { Worker, Job } from 'bullmq';
import { ConfigService } from '@nestjs/config';
import { getRedisConnection } from '../../../common/utils/parse-redis-url';
import { DocumentProcessorService, ChunkingConfig } from '../document-processor.service';
import { VectorStoreService } from '../vector-store.service';
import { RAGEngineService } from '../rag-engine.service';
import { RedisService } from '../../../common/services/redis.service';

// ---------- Types ----------

export interface ProcessDocumentJobData {
  tenantId: string;
  documentId: string;
  knowledgeBaseId: string;
  fileKey: string;
  chunkingConfig?: Partial<ChunkingConfig>;
}

export interface ReindexDocumentJobData {
  tenantId: string;
  documentId: string;
  knowledgeBaseId: string;
  fileKey: string;
  chunkingConfig?: Partial<ChunkingConfig>;
}

export interface DeleteEmbeddingsJobData {
  tenantId: string;
  documentId?: string;
  knowledgeBaseId?: string;
}

type DocumentJobData =
  | ProcessDocumentJobData
  | ReindexDocumentJobData
  | DeleteEmbeddingsJobData;

// ---------- Processor ----------

export class DocumentProcessor {
  private readonly logger = new Logger(DocumentProcessor.name);
  private worker: Worker | null = null;

  constructor(
    private readonly documentProcessorService: DocumentProcessorService,
    private readonly vectorStoreService: VectorStoreService,
    private readonly ragEngineService: RAGEngineService,
    private readonly redisService: RedisService,
    private readonly configService: ConfigService,
  ) {}

  async start(): Promise<void> {
    const connection = getRedisConnection(this.configService);

    this.worker = new Worker(
      'kb-processing',
      async (job: Job<DocumentJobData>) => {
        return this.processJob(job);
      },
      {
        connection,
        concurrency: this.configService.get<number>('KB_PROCESSOR_CONCURRENCY', 3),
        limiter: {
          max: this.configService.get<number>('KB_PROCESSOR_RATE_LIMIT', 10),
          duration: 60000,
        },
      },
    );

    this.worker.on('completed', (job: Job) => {
      this.logger.log(`Job ${job.name} (${job.id}) completed`);
    });

    this.worker.on('failed', (job: Job | undefined, error: Error) => {
      this.logger.error(
        `Job ${job?.name} (${job?.id}) failed: ${error.message}`,
        error.stack,
      );
    });

    this.worker.on('progress', (job: Job, progress: number | object) => {
      this.logger.debug(
        `Job ${job.name} (${job.id}) progress: ${JSON.stringify(progress)}`,
      );
    });

    this.logger.log('Document processor worker started');
  }

  async stop(): Promise<void> {
    if (this.worker) {
      await this.worker.close();
      this.worker = null;
      this.logger.log('Document processor worker stopped');
    }
  }

  // ---------- Job Routing ----------

  private async processJob(job: Job<DocumentJobData>): Promise<unknown> {
    this.logger.log(
      `Processing job ${job.name} (${job.id}) for tenant ${job.data.tenantId}`,
    );

    switch (job.name) {
      case 'process-document':
        return this.handleProcessDocument(job as Job<ProcessDocumentJobData>);

      case 'reindex-document':
        return this.handleReindexDocument(job as Job<ReindexDocumentJobData>);

      case 'delete-embeddings':
        return this.handleDeleteEmbeddings(job as Job<DeleteEmbeddingsJobData>);

      default:
        this.logger.warn(`Unknown job type: ${job.name}`);
        throw new Error(`Unknown document processing job type: ${job.name}`);
    }
  }

  // ---------- Job Handlers ----------

  private async handleProcessDocument(
    job: Job<ProcessDocumentJobData>,
  ): Promise<unknown> {
    const { tenantId, documentId, knowledgeBaseId, fileKey, chunkingConfig } = job.data;

    await job.updateProgress(0);

    // Publish initial status via WebSocket (through Redis pub/sub)
    await this.publishWebSocketEvent(tenantId, {
      type: 'document:processing:started',
      documentId,
      knowledgeBaseId,
    });

    try {
      const result = await this.documentProcessorService.processDocument(
        tenantId,
        documentId,
        knowledgeBaseId,
        fileKey,
        chunkingConfig,
      );

      await job.updateProgress(100);

      // Invalidate RAG cache for this knowledge base
      await this.ragEngineService.invalidateCache(tenantId, knowledgeBaseId);

      // Publish completion via WebSocket
      await this.publishWebSocketEvent(tenantId, {
        type: 'document:processing:completed',
        documentId,
        knowledgeBaseId,
        result: {
          chunksCreated: result.chunksCreated,
          embeddingsGenerated: result.embeddingsGenerated,
          processingTimeMs: result.processingTimeMs,
        },
      });

      return result;
    } catch (error) {
      await this.publishWebSocketEvent(tenantId, {
        type: 'document:processing:failed',
        documentId,
        knowledgeBaseId,
        error: (error as Error).message,
      });

      throw error;
    }
  }

  private async handleReindexDocument(
    job: Job<ReindexDocumentJobData>,
  ): Promise<unknown> {
    const { tenantId, documentId, knowledgeBaseId, fileKey, chunkingConfig } = job.data;

    await this.publishWebSocketEvent(tenantId, {
      type: 'document:reindexing:started',
      documentId,
      knowledgeBaseId,
    });

    try {
      const result = await this.documentProcessorService.reindexDocument(
        tenantId,
        documentId,
        knowledgeBaseId,
        fileKey,
        chunkingConfig,
      );

      await this.ragEngineService.invalidateCache(tenantId, knowledgeBaseId);

      await this.publishWebSocketEvent(tenantId, {
        type: 'document:reindexing:completed',
        documentId,
        knowledgeBaseId,
        result: {
          chunksCreated: result.chunksCreated,
          embeddingsGenerated: result.embeddingsGenerated,
        },
      });

      return result;
    } catch (error) {
      await this.publishWebSocketEvent(tenantId, {
        type: 'document:reindexing:failed',
        documentId,
        knowledgeBaseId,
        error: (error as Error).message,
      });

      throw error;
    }
  }

  private async handleDeleteEmbeddings(
    job: Job<DeleteEmbeddingsJobData>,
  ): Promise<unknown> {
    const { tenantId, documentId, knowledgeBaseId } = job.data;

    let deletedCount = 0;

    if (documentId) {
      deletedCount = await this.vectorStoreService.deleteEmbeddingsByDocument(documentId);
      this.logger.log(`Deleted ${deletedCount} embeddings for document ${documentId}`);
    } else if (knowledgeBaseId) {
      deletedCount = await this.vectorStoreService.deleteEmbeddingsByKnowledgeBase(knowledgeBaseId);
      this.logger.log(
        `Deleted ${deletedCount} embeddings for knowledge base ${knowledgeBaseId}`,
      );
    }

    await this.ragEngineService.invalidateCache(tenantId, knowledgeBaseId);

    await this.publishWebSocketEvent(tenantId, {
      type: 'embeddings:deleted',
      documentId,
      knowledgeBaseId,
      deletedCount,
    });

    return { deletedCount };
  }

  // ---------- WebSocket Events ----------

  private async publishWebSocketEvent(
    tenantId: string,
    event: Record<string, unknown>,
  ): Promise<void> {
    try {
      const client = this.redisService.getClient();
      await client.publish(
        'kb:events',
        JSON.stringify({ tenantId, ...event, timestamp: new Date().toISOString() }),
      );
    } catch (error) {
      this.logger.warn(`Failed to publish WebSocket event: ${(error as Error).message}`);
    }
  }
}
