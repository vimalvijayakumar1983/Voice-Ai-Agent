import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { PrismaService } from '../../common/services/prisma.service';
import { S3Service } from '../../common/services/s3.service';
import { RedisService } from '../../common/services/redis.service';
import { EmbeddingService } from '../../services/embeddings/embedding.service';
import { EmbeddingInputType } from '../../services/embeddings/embedding.interface';
import { Readable } from 'stream';

// ---------- Types ----------

export enum ChunkingStrategy {
  FIXED_SIZE = 'fixed_size',
  SENTENCE = 'sentence',
  SEMANTIC = 'semantic',
}

export interface ChunkingConfig {
  strategy: ChunkingStrategy;
  chunkSize: number;
  chunkOverlap: number;
  minChunkSize: number;
  maxChunkSize: number;
}

export interface DocumentChunk {
  content: string;
  metadata: ChunkMetadata;
  index: number;
}

export interface ChunkMetadata {
  documentId: string;
  knowledgeBaseId: string;
  tenantId: string;
  fileName: string;
  chunkIndex: number;
  startOffset: number;
  endOffset: number;
  pageNumber?: number;
  section?: string;
  headings?: string[];
}

export interface DocumentProcessingResult {
  documentId: string;
  chunksCreated: number;
  embeddingsGenerated: number;
  totalTokens: number;
  processingTimeMs: number;
}

export interface ProcessingProgress {
  documentId: string;
  stage: 'parsing' | 'chunking' | 'embedding' | 'storing' | 'complete' | 'failed';
  progress: number;
  message: string;
  chunksProcessed?: number;
  totalChunks?: number;
}

const SUPPORTED_FORMATS = new Set([
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'text/plain',
  'text/csv',
  'text/html',
  'text/markdown',
]);

const DEFAULT_CHUNKING_CONFIG: ChunkingConfig = {
  strategy: ChunkingStrategy.SENTENCE,
  chunkSize: 512,
  chunkOverlap: 64,
  minChunkSize: 100,
  maxChunkSize: 2048,
};

// ---------- Service ----------

@Injectable()
export class DocumentProcessorService {
  private readonly logger = new Logger(DocumentProcessorService.name);
  private readonly embeddingBatchSize: number;

  constructor(
    private readonly prisma: PrismaService,
    private readonly s3Service: S3Service,
    private readonly redisService: RedisService,
    private readonly embeddingService: EmbeddingService,
    private readonly configService: ConfigService,
  ) {
    this.embeddingBatchSize = this.configService.get<number>(
      'KB_EMBEDDING_BATCH_SIZE',
      50,
    );
  }

  // ---------- Main Pipeline ----------

  async processDocument(
    tenantId: string,
    documentId: string,
    knowledgeBaseId: string,
    fileKey: string,
    chunkingConfig?: Partial<ChunkingConfig>,
  ): Promise<DocumentProcessingResult> {
    const startTime = Date.now();
    const config = { ...DEFAULT_CHUNKING_CONFIG, ...chunkingConfig };

    try {
      // 1. Update status
      await this.updateDocumentStatus(documentId, 'PROCESSING');
      await this.reportProgress(documentId, 'parsing', 0, 'Downloading document...');

      // 2. Parse document
      const document = await this.prisma.kbDocument.findUnique({
        where: { id: documentId },
      });
      if (!document) {
        throw new Error(`Document ${documentId} not found`);
      }

      const rawContent = await this.downloadAndParse(fileKey, document.contentType);
      await this.reportProgress(documentId, 'parsing', 20, 'Document parsed successfully');

      // 3. Extract metadata
      const docMetadata = this.extractMetadata(rawContent, document.fileName);

      // 4. Chunk the document
      await this.reportProgress(documentId, 'chunking', 30, 'Splitting into chunks...');
      const chunks = this.chunkDocument(rawContent, config, {
        documentId,
        knowledgeBaseId,
        tenantId,
        fileName: document.fileName,
        chunkIndex: 0,
        startOffset: 0,
        endOffset: 0,
      });

      this.logger.log(
        `Document ${documentId}: created ${chunks.length} chunks with strategy '${config.strategy}'`,
      );
      await this.reportProgress(
        documentId,
        'chunking',
        40,
        `Created ${chunks.length} chunks`,
      );

      // 5. Generate embeddings in batches
      await this.reportProgress(documentId, 'embedding', 45, 'Generating embeddings...');
      let totalTokens = 0;
      const embeddedChunks: Array<{ chunk: DocumentChunk; vector: number[] }> = [];

      for (let i = 0; i < chunks.length; i += this.embeddingBatchSize) {
        const batch = chunks.slice(i, i + this.embeddingBatchSize);
        const texts = batch.map((c) => c.content);

        const batchResult = await this.embeddingService.embedBatch(texts, {
          inputType: EmbeddingInputType.SEARCH_DOCUMENT,
        });

        for (let j = 0; j < batch.length; j++) {
          embeddedChunks.push({
            chunk: batch[j],
            vector: batchResult.embeddings[j].vector,
          });
        }

        totalTokens += batchResult.totalTokens;

        const embeddingProgress = 45 + Math.round(((i + batch.length) / chunks.length) * 35);
        await this.reportProgress(
          documentId,
          'embedding',
          embeddingProgress,
          `Embedded ${Math.min(i + this.embeddingBatchSize, chunks.length)} of ${chunks.length} chunks`,
        );
      }

      // 6. Store chunks and embeddings in database
      await this.reportProgress(documentId, 'storing', 85, 'Storing embeddings...');
      await this.storeChunksAndEmbeddings(embeddedChunks);

      // 7. Update document metadata and status
      await this.prisma.kbDocument.update({
        where: { id: documentId },
        data: {
          status: 'READY',
          metadata: {
            ...(document.metadata as Record<string, unknown> || {}),
            ...docMetadata,
            chunksCount: chunks.length,
            totalTokens,
            processingTimeMs: Date.now() - startTime,
          },
        },
      });

      await this.reportProgress(documentId, 'complete', 100, 'Processing complete');

      const result: DocumentProcessingResult = {
        documentId,
        chunksCreated: chunks.length,
        embeddingsGenerated: embeddedChunks.length,
        totalTokens,
        processingTimeMs: Date.now() - startTime,
      };

      this.logger.log(
        `Document ${documentId} processed: ${result.chunksCreated} chunks, ${result.totalTokens} tokens, ${result.processingTimeMs}ms`,
      );

      return result;
    } catch (error) {
      this.logger.error(
        `Failed to process document ${documentId}: ${(error as Error).message}`,
        (error as Error).stack,
      );

      await this.updateDocumentStatus(documentId, 'FAILED');
      await this.reportProgress(
        documentId,
        'failed',
        0,
        `Processing failed: ${(error as Error).message}`,
      );

      throw error;
    }
  }

  async reindexDocument(
    tenantId: string,
    documentId: string,
    knowledgeBaseId: string,
    fileKey: string,
    chunkingConfig?: Partial<ChunkingConfig>,
  ): Promise<DocumentProcessingResult> {
    this.logger.log(`Re-indexing document ${documentId}`);

    // Delete existing embeddings
    await this.deleteDocumentEmbeddings(documentId);

    // Reprocess
    return this.processDocument(tenantId, documentId, knowledgeBaseId, fileKey, chunkingConfig);
  }

  async deleteDocumentEmbeddings(documentId: string): Promise<number> {
    const result = await this.prisma.kbChunk.deleteMany({
      where: { documentId },
    });

    this.logger.log(
      `Deleted ${result.count} chunks/embeddings for document ${documentId}`,
    );

    return result.count;
  }

  // ---------- Parsing ----------

  private async downloadAndParse(fileKey: string, contentType: string): Promise<string> {
    if (!SUPPORTED_FORMATS.has(contentType)) {
      throw new Error(`Unsupported file format: ${contentType}`);
    }

    const stream = await this.s3Service.download(fileKey);
    const buffer = await this.streamToBuffer(stream);

    switch (contentType) {
      case 'text/plain':
      case 'text/csv':
      case 'text/markdown':
        return buffer.toString('utf-8');

      case 'text/html':
        return this.parseHTML(buffer.toString('utf-8'));

      case 'application/pdf':
        return this.parsePDF(buffer);

      case 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
        return this.parseDOCX(buffer);

      default:
        return buffer.toString('utf-8');
    }
  }

  private parseHTML(html: string): string {
    // Strip HTML tags, decode entities, normalize whitespace
    let text = html
      .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, '')
      .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, '')
      .replace(/<nav[^>]*>[\s\S]*?<\/nav>/gi, '')
      .replace(/<footer[^>]*>[\s\S]*?<\/footer>/gi, '')
      .replace(/<header[^>]*>[\s\S]*?<\/header>/gi, '');

    // Convert block elements to newlines
    text = text
      .replace(/<\/?(h[1-6]|p|div|br|li|tr|td|th|blockquote|pre)[^>]*>/gi, '\n')
      .replace(/<\/?[^>]+(>|$)/g, '')
      .replace(/&nbsp;/gi, ' ')
      .replace(/&amp;/gi, '&')
      .replace(/&lt;/gi, '<')
      .replace(/&gt;/gi, '>')
      .replace(/&quot;/gi, '"')
      .replace(/&#39;/gi, "'");

    return this.normalizeWhitespace(text);
  }

  private async parsePDF(buffer: Buffer): Promise<string> {
    // Use a lightweight PDF text extraction approach
    // In production, integrate with pdf-parse or similar library
    // For now, extract text content from the PDF buffer
    try {
      // Dynamic import for pdf-parse if available
      const pdfParse = await this.tryImport('pdf-parse');
      if (pdfParse) {
        const result = await pdfParse(buffer);
        return this.normalizeWhitespace(result.text);
      }
    } catch {
      this.logger.warn('pdf-parse not available, attempting basic extraction');
    }

    // Basic fallback: extract readable ASCII strings from PDF
    const text = buffer
      .toString('utf-8')
      .replace(/[^\x20-\x7E\n\r\t]/g, ' ');
    return this.normalizeWhitespace(text);
  }

  private async parseDOCX(buffer: Buffer): Promise<string> {
    // In production, use mammoth or similar library
    try {
      const mammoth = await this.tryImport('mammoth');
      if (mammoth) {
        const result = await mammoth.extractRawText({ buffer });
        return this.normalizeWhitespace(result.value);
      }
    } catch {
      this.logger.warn('mammoth not available, attempting basic DOCX extraction');
    }

    // DOCX files are ZIP archives containing XML; attempt basic extraction
    // of text content from the XML payload
    const text = buffer
      .toString('utf-8')
      .replace(/<[^>]+>/g, ' ')
      .replace(/[^\x20-\x7E\n\r\t]/g, ' ');
    return this.normalizeWhitespace(text);
  }

  private async tryImport(moduleName: string): Promise<any> {
    try {
      return await import(moduleName);
    } catch {
      return null;
    }
  }

  // ---------- Chunking Strategies ----------

  chunkDocument(
    content: string,
    config: ChunkingConfig,
    baseMetadata: ChunkMetadata,
  ): DocumentChunk[] {
    switch (config.strategy) {
      case ChunkingStrategy.FIXED_SIZE:
        return this.fixedSizeChunk(content, config, baseMetadata);
      case ChunkingStrategy.SENTENCE:
        return this.sentenceChunk(content, config, baseMetadata);
      case ChunkingStrategy.SEMANTIC:
        return this.semanticChunk(content, config, baseMetadata);
      default:
        return this.sentenceChunk(content, config, baseMetadata);
    }
  }

  private fixedSizeChunk(
    content: string,
    config: ChunkingConfig,
    baseMetadata: ChunkMetadata,
  ): DocumentChunk[] {
    const chunks: DocumentChunk[] = [];
    const { chunkSize, chunkOverlap, minChunkSize } = config;
    const step = chunkSize - chunkOverlap;
    let index = 0;

    for (let start = 0; start < content.length; start += step) {
      const end = Math.min(start + chunkSize, content.length);
      const chunkContent = content.slice(start, end).trim();

      if (chunkContent.length < minChunkSize && chunks.length > 0) {
        // Append to previous chunk if too small
        const prev = chunks[chunks.length - 1];
        prev.content += ' ' + chunkContent;
        prev.metadata.endOffset = end;
        continue;
      }

      if (chunkContent.length >= minChunkSize) {
        chunks.push({
          content: chunkContent,
          metadata: {
            ...baseMetadata,
            chunkIndex: index,
            startOffset: start,
            endOffset: end,
          },
          index,
        });
        index++;
      }
    }

    return chunks;
  }

  private sentenceChunk(
    content: string,
    config: ChunkingConfig,
    baseMetadata: ChunkMetadata,
  ): DocumentChunk[] {
    const sentences = this.splitIntoSentences(content);
    const chunks: DocumentChunk[] = [];
    const { chunkSize, chunkOverlap, minChunkSize, maxChunkSize } = config;

    let currentChunk: string[] = [];
    let currentLength = 0;
    let chunkIndex = 0;
    let startOffset = 0;
    let currentOffset = 0;

    for (const sentence of sentences) {
      const sentenceLength = sentence.length;

      // If a single sentence exceeds maxChunkSize, split it
      if (sentenceLength > maxChunkSize) {
        // Flush current chunk first
        if (currentChunk.length > 0) {
          const chunkContent = currentChunk.join(' ').trim();
          if (chunkContent.length >= minChunkSize) {
            chunks.push({
              content: chunkContent,
              metadata: {
                ...baseMetadata,
                chunkIndex,
                startOffset,
                endOffset: currentOffset,
              },
              index: chunkIndex,
            });
            chunkIndex++;
          }
          currentChunk = [];
          currentLength = 0;
        }

        // Split long sentence into fixed-size sub-chunks
        const subChunks = this.fixedSizeChunk(sentence, config, {
          ...baseMetadata,
          chunkIndex,
          startOffset: currentOffset,
          endOffset: currentOffset + sentenceLength,
        });
        for (const sc of subChunks) {
          sc.index = chunkIndex;
          sc.metadata.chunkIndex = chunkIndex;
          chunks.push(sc);
          chunkIndex++;
        }

        currentOffset += sentenceLength;
        startOffset = currentOffset;
        continue;
      }

      if (currentLength + sentenceLength > chunkSize && currentChunk.length > 0) {
        // Flush current chunk
        const chunkContent = currentChunk.join(' ').trim();
        if (chunkContent.length >= minChunkSize) {
          chunks.push({
            content: chunkContent,
            metadata: {
              ...baseMetadata,
              chunkIndex,
              startOffset,
              endOffset: currentOffset,
            },
            index: chunkIndex,
          });
          chunkIndex++;
        }

        // Calculate overlap: keep last N characters worth of sentences
        const overlapSentences: string[] = [];
        let overlapLen = 0;
        for (let i = currentChunk.length - 1; i >= 0 && overlapLen < chunkOverlap; i--) {
          overlapSentences.unshift(currentChunk[i]);
          overlapLen += currentChunk[i].length;
        }

        currentChunk = overlapSentences;
        currentLength = overlapLen;
        startOffset = currentOffset - overlapLen;
      }

      currentChunk.push(sentence);
      currentLength += sentenceLength;
      currentOffset += sentenceLength;
    }

    // Flush remaining
    if (currentChunk.length > 0) {
      const chunkContent = currentChunk.join(' ').trim();
      if (chunkContent.length >= minChunkSize) {
        chunks.push({
          content: chunkContent,
          metadata: {
            ...baseMetadata,
            chunkIndex,
            startOffset,
            endOffset: currentOffset,
          },
          index: chunkIndex,
        });
      }
    }

    return chunks;
  }

  private semanticChunk(
    content: string,
    config: ChunkingConfig,
    baseMetadata: ChunkMetadata,
  ): DocumentChunk[] {
    // Semantic chunking: split on structural boundaries (headers, double newlines, etc.)
    // then merge or split to achieve target chunk size
    const sections = content.split(/\n{2,}|\r\n{2,}/);
    const chunks: DocumentChunk[] = [];
    const { chunkSize, chunkOverlap, minChunkSize, maxChunkSize } = config;

    let currentContent = '';
    let chunkIndex = 0;
    let startOffset = 0;
    let currentOffset = 0;

    for (const section of sections) {
      const trimmed = section.trim();
      if (!trimmed) {
        currentOffset += section.length;
        continue;
      }

      if (currentContent.length + trimmed.length + 1 > maxChunkSize && currentContent.length > 0) {
        // Flush
        if (currentContent.length >= minChunkSize) {
          chunks.push({
            content: currentContent.trim(),
            metadata: {
              ...baseMetadata,
              chunkIndex,
              startOffset,
              endOffset: currentOffset,
              section: this.detectSectionHeading(currentContent),
            },
            index: chunkIndex,
          });
          chunkIndex++;
        }

        // Overlap
        const overlapStart = Math.max(0, currentContent.length - chunkOverlap);
        currentContent = currentContent.slice(overlapStart);
        startOffset = currentOffset - currentContent.length;
      }

      if (currentContent.length > 0) {
        currentContent += '\n\n' + trimmed;
      } else {
        currentContent = trimmed;
        startOffset = currentOffset;
      }

      currentOffset += section.length;
    }

    // Flush remaining
    if (currentContent.trim().length >= minChunkSize) {
      chunks.push({
        content: currentContent.trim(),
        metadata: {
          ...baseMetadata,
          chunkIndex,
          startOffset,
          endOffset: currentOffset,
          section: this.detectSectionHeading(currentContent),
        },
        index: chunkIndex,
      });
    }

    return chunks;
  }

  // ---------- Helpers ----------

  private splitIntoSentences(text: string): string[] {
    // Split on sentence boundaries: period/question/exclamation followed by space and uppercase
    const sentenceRegex = /(?<=[.!?])\s+(?=[A-Z])/g;
    const raw = text.split(sentenceRegex).filter((s) => s.trim().length > 0);

    // Also split on newlines to respect paragraph boundaries
    const result: string[] = [];
    for (const segment of raw) {
      const lines = segment.split(/\n+/);
      for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed.length > 0) {
          result.push(trimmed);
        }
      }
    }

    return result;
  }

  private detectSectionHeading(content: string): string | undefined {
    const firstLine = content.split('\n')[0]?.trim();
    if (!firstLine) return undefined;

    // Detect markdown-style headings
    const mdMatch = firstLine.match(/^#{1,6}\s+(.+)$/);
    if (mdMatch) return mdMatch[1];

    // Detect short all-caps or title-case lines as potential headings
    if (firstLine.length < 100 && firstLine === firstLine.toUpperCase()) {
      return firstLine;
    }

    return undefined;
  }

  extractMetadata(
    content: string,
    fileName: string,
  ): Record<string, unknown> {
    const wordCount = content.split(/\s+/).filter(Boolean).length;
    const lineCount = content.split('\n').length;
    const charCount = content.length;

    // Detect headings
    const headings: string[] = [];
    const headingRegex = /^#{1,6}\s+(.+)$/gm;
    let match: RegExpExecArray | null;
    while ((match = headingRegex.exec(content)) !== null) {
      headings.push(match[1]);
    }

    // Detect language (simple heuristic)
    const hasJapanese = /[\u3040-\u309F\u30A0-\u30FF]/.test(content);
    const hasChinese = /[\u4E00-\u9FFF]/.test(content);
    const detectedLanguage = hasJapanese ? 'ja' : hasChinese ? 'zh' : 'en';

    return {
      wordCount,
      lineCount,
      charCount,
      headings: headings.slice(0, 50),
      detectedLanguage,
      fileName,
      processedAt: new Date().toISOString(),
    };
  }

  private async storeChunksAndEmbeddings(
    embeddedChunks: Array<{ chunk: DocumentChunk; vector: number[] }>,
  ): Promise<void> {
    // Batch insert chunks with embeddings
    const batchSize = 100;

    for (let i = 0; i < embeddedChunks.length; i += batchSize) {
      const batch = embeddedChunks.slice(i, i + batchSize);

      await this.prisma.$transaction(
        batch.map(({ chunk, vector }) =>
          this.prisma.kbChunk.create({
            data: {
              knowledgeBaseId: chunk.metadata.knowledgeBaseId,
              documentId: chunk.metadata.documentId,
              content: chunk.content,
              chunkIndex: chunk.index,
              embedding: vector,
              metadata: chunk.metadata as any,
              tenantId: chunk.metadata.tenantId,
            },
          }),
        ),
      );
    }
  }

  private async updateDocumentStatus(documentId: string, status: string): Promise<void> {
    await this.prisma.kbDocument.update({
      where: { id: documentId },
      data: { status },
    });
  }

  async reportProgress(
    documentId: string,
    stage: ProcessingProgress['stage'],
    progress: number,
    message: string,
    extra?: Partial<ProcessingProgress>,
  ): Promise<void> {
    const progressData: ProcessingProgress = {
      documentId,
      stage,
      progress,
      message,
      ...extra,
    };

    // Store progress in Redis for WebSocket consumers
    await this.redisService.setJson(
      `kb:doc:progress:${documentId}`,
      progressData,
      3600,
    );

    // Publish to Redis pub/sub for real-time updates
    const client = this.redisService.getClient();
    await client.publish(
      'kb:document:progress',
      JSON.stringify(progressData),
    );
  }

  private normalizeWhitespace(text: string): string {
    return text
      .replace(/\r\n/g, '\n')
      .replace(/[ \t]+/g, ' ')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  private async streamToBuffer(stream: Readable): Promise<Buffer> {
    const chunks: Buffer[] = [];
    for await (const chunk of stream) {
      chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    }
    return Buffer.concat(chunks);
  }
}
