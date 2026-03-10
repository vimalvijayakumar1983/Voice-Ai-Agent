import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { PrismaService } from '../../common/services/prisma.service';
import { RedisService } from '../../common/services/redis.service';
import { EmbeddingService } from '../../services/embeddings/embedding.service';
import { EmbeddingInputType } from '../../services/embeddings/embedding.interface';

// ---------- Types ----------

export interface VectorSearchOptions {
  topK: number;
  minScore?: number;
  knowledgeBaseId?: string;
  documentIds?: string[];
  metadataFilter?: Record<string, unknown>;
  includeContent?: boolean;
  includeMetadata?: boolean;
}

export interface VectorSearchResult {
  id: string;
  content: string;
  score: number;
  documentId: string;
  knowledgeBaseId: string;
  chunkIndex: number;
  metadata: Record<string, unknown>;
}

export interface HybridSearchOptions extends VectorSearchOptions {
  vectorWeight?: number;
  textWeight?: number;
  fullTextQuery?: string;
}

export interface MMROptions extends VectorSearchOptions {
  lambda?: number;
  fetchK?: number;
}

// ---------- Service ----------

@Injectable()
export class VectorStoreService {
  private readonly logger = new Logger(VectorStoreService.name);
  private readonly defaultTopK: number;
  private readonly defaultMinScore: number;

  constructor(
    private readonly prisma: PrismaService,
    private readonly redisService: RedisService,
    private readonly embeddingService: EmbeddingService,
    private readonly configService: ConfigService,
  ) {
    this.defaultTopK = this.configService.get<number>('VECTOR_SEARCH_TOP_K', 10);
    this.defaultMinScore = this.configService.get<number>('VECTOR_SEARCH_MIN_SCORE', 0.5);
  }

  // ---------- Cosine Similarity Search ----------

  async searchByVector(
    tenantId: string,
    queryVector: number[],
    options: VectorSearchOptions,
  ): Promise<VectorSearchResult[]> {
    const topK = options.topK || this.defaultTopK;
    const minScore = options.minScore ?? this.defaultMinScore;

    // Build WHERE conditions for the raw query
    const conditions: string[] = [`c."tenantId" = $1`];
    const params: unknown[] = [tenantId];
    let paramIndex = 2;

    if (options.knowledgeBaseId) {
      conditions.push(`c."knowledgeBaseId" = $${paramIndex}`);
      params.push(options.knowledgeBaseId);
      paramIndex++;
    }

    if (options.documentIds && options.documentIds.length > 0) {
      conditions.push(`c."documentId" = ANY($${paramIndex}::text[])`);
      params.push(options.documentIds);
      paramIndex++;
    }

    // pgvector cosine distance: 1 - (embedding <=> query_vector) = cosine similarity
    const vectorParam = `$${paramIndex}`;
    params.push(`[${queryVector.join(',')}]`);
    paramIndex++;

    const whereClause = conditions.join(' AND ');

    const query = `
      SELECT
        c.id,
        c.content,
        c."documentId",
        c."knowledgeBaseId",
        c."chunkIndex",
        c.metadata,
        1 - (c.embedding::vector <=> ${vectorParam}::vector) AS score
      FROM "KbChunk" c
      WHERE ${whereClause}
        AND 1 - (c.embedding::vector <=> ${vectorParam}::vector) >= $${paramIndex}
      ORDER BY c.embedding::vector <=> ${vectorParam}::vector ASC
      LIMIT $${paramIndex + 1}
    `;

    params.push(minScore);
    params.push(topK);

    const results = await this.prisma.$queryRawUnsafe<any[]>(query, ...params);

    return results.map((row) => ({
      id: row.id,
      content: row.content,
      score: parseFloat(row.score),
      documentId: row.documentId,
      knowledgeBaseId: row.knowledgeBaseId,
      chunkIndex: row.chunkIndex,
      metadata: typeof row.metadata === 'string' ? JSON.parse(row.metadata) : row.metadata || {},
    }));
  }

  // ---------- Text-Based Search ----------

  async searchByText(
    tenantId: string,
    queryText: string,
    options: VectorSearchOptions,
  ): Promise<VectorSearchResult[]> {
    // Embed the query first
    const queryEmbedding = await this.embeddingService.embed(queryText, {
      inputType: EmbeddingInputType.SEARCH_QUERY,
    });

    return this.searchByVector(tenantId, queryEmbedding.vector, options);
  }

  // ---------- Hybrid Search (Vector + Full-Text) ----------

  async hybridSearch(
    tenantId: string,
    queryText: string,
    options: HybridSearchOptions,
  ): Promise<VectorSearchResult[]> {
    const vectorWeight = options.vectorWeight ?? 0.7;
    const textWeight = options.textWeight ?? 0.3;
    const topK = options.topK || this.defaultTopK;
    const fetchK = topK * 3; // Fetch more candidates for reranking

    // 1. Vector search
    const queryEmbedding = await this.embeddingService.embed(queryText, {
      inputType: EmbeddingInputType.SEARCH_QUERY,
    });

    const vectorResults = await this.searchByVector(tenantId, queryEmbedding.vector, {
      ...options,
      topK: fetchK,
      minScore: 0.3, // Lower threshold for candidates
    });

    // 2. Full-text search using PostgreSQL's tsvector
    const fullTextQuery = options.fullTextQuery || queryText;
    const tsQuery = fullTextQuery
      .split(/\s+/)
      .filter(Boolean)
      .map((word) => word.replace(/[^\w]/g, ''))
      .filter((w) => w.length > 1)
      .join(' & ');

    let fullTextResults: Array<{ id: string; rank: number }> = [];

    if (tsQuery) {
      const conditions: string[] = [`c."tenantId" = $1`];
      const params: unknown[] = [tenantId];
      let paramIndex = 2;

      if (options.knowledgeBaseId) {
        conditions.push(`c."knowledgeBaseId" = $${paramIndex}`);
        params.push(options.knowledgeBaseId);
        paramIndex++;
      }

      params.push(tsQuery);
      const whereClause = conditions.join(' AND ');

      const ftQuery = `
        SELECT
          c.id,
          ts_rank_cd(to_tsvector('english', c.content), to_tsquery('english', $${paramIndex})) AS rank
        FROM "KbChunk" c
        WHERE ${whereClause}
          AND to_tsvector('english', c.content) @@ to_tsquery('english', $${paramIndex})
        ORDER BY rank DESC
        LIMIT $${paramIndex + 1}
      `;

      params.push(fetchK);

      fullTextResults = await this.prisma.$queryRawUnsafe<Array<{ id: string; rank: number }>>(
        ftQuery,
        ...params,
      );
    }

    // 3. Combine scores
    const combinedScores = new Map<string, { result: VectorSearchResult; hybridScore: number }>();

    // Normalize vector scores
    const maxVectorScore = Math.max(...vectorResults.map((r) => r.score), 0.001);
    for (const result of vectorResults) {
      const normalizedVectorScore = result.score / maxVectorScore;
      combinedScores.set(result.id, {
        result,
        hybridScore: normalizedVectorScore * vectorWeight,
      });
    }

    // Add normalized full-text scores
    const maxFtRank = Math.max(...fullTextResults.map((r) => r.rank), 0.001);
    for (const ftResult of fullTextResults) {
      const normalizedFtScore = ftResult.rank / maxFtRank;
      const existing = combinedScores.get(ftResult.id);
      if (existing) {
        existing.hybridScore += normalizedFtScore * textWeight;
      }
      // Only include results that also had vector matches (for quality)
    }

    // 4. Sort by hybrid score and return top-K
    const sorted = Array.from(combinedScores.values())
      .sort((a, b) => b.hybridScore - a.hybridScore)
      .slice(0, topK);

    return sorted.map(({ result, hybridScore }) => ({
      ...result,
      score: hybridScore,
    }));
  }

  // ---------- MMR (Maximum Marginal Relevance) ----------

  async mmrSearch(
    tenantId: string,
    queryText: string,
    options: MMROptions,
  ): Promise<VectorSearchResult[]> {
    const lambda = options.lambda ?? 0.5;
    const topK = options.topK || this.defaultTopK;
    const fetchK = options.fetchK || topK * 4;

    // 1. Get query embedding
    const queryEmbedding = await this.embeddingService.embed(queryText, {
      inputType: EmbeddingInputType.SEARCH_QUERY,
    });

    // 2. Fetch candidate results
    const candidates = await this.searchByVector(tenantId, queryEmbedding.vector, {
      ...options,
      topK: fetchK,
      minScore: 0.3,
    });

    if (candidates.length === 0) return [];

    // 3. MMR selection
    // We need the actual embeddings for diversity calculation
    // For efficiency, we approximate using content similarity
    const selected: VectorSearchResult[] = [];
    const remaining = [...candidates];

    // Select first result (highest similarity to query)
    selected.push(remaining.shift()!);

    while (selected.length < topK && remaining.length > 0) {
      let bestIdx = -1;
      let bestMmrScore = -Infinity;

      for (let i = 0; i < remaining.length; i++) {
        const candidate = remaining[i];

        // Relevance to query
        const relevance = candidate.score;

        // Maximum similarity to already selected results (using content-based approximation)
        let maxSimilarityToSelected = 0;
        for (const sel of selected) {
          const similarity = this.computeJaccardSimilarity(
            candidate.content,
            sel.content,
          );
          maxSimilarityToSelected = Math.max(maxSimilarityToSelected, similarity);
        }

        // MMR score: lambda * relevance - (1 - lambda) * max_sim_to_selected
        const mmrScore = lambda * relevance - (1 - lambda) * maxSimilarityToSelected;

        if (mmrScore > bestMmrScore) {
          bestMmrScore = mmrScore;
          bestIdx = i;
        }
      }

      if (bestIdx >= 0) {
        selected.push(remaining[bestIdx]);
        remaining.splice(bestIdx, 1);
      } else {
        break;
      }
    }

    return selected;
  }

  // ---------- Reranking ----------

  rerank(
    results: VectorSearchResult[],
    queryText: string,
    topK?: number,
  ): VectorSearchResult[] {
    // BM25-style reranking based on query term overlap
    const queryTerms = new Set(
      queryText
        .toLowerCase()
        .split(/\s+/)
        .filter((w) => w.length > 2),
    );

    const avgDocLength =
      results.reduce((sum, r) => sum + r.content.length, 0) / Math.max(results.length, 1);

    const reranked = results.map((result) => {
      const docTerms = result.content.toLowerCase().split(/\s+/);
      const docLength = docTerms.length;

      // BM25 parameters
      const k1 = 1.2;
      const b = 0.75;

      let bm25Score = 0;
      for (const queryTerm of queryTerms) {
        const tf = docTerms.filter((t) => t.includes(queryTerm)).length;
        const idf = Math.log(
          (results.length - results.filter((r) =>
            r.content.toLowerCase().includes(queryTerm),
          ).length + 0.5) /
          (results.filter((r) =>
            r.content.toLowerCase().includes(queryTerm),
          ).length + 0.5) + 1,
        );

        const tfNorm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * (docLength / avgDocLength)));
        bm25Score += idf * tfNorm;
      }

      // Combine original vector score with BM25
      const combinedScore = 0.6 * result.score + 0.4 * Math.min(bm25Score / 10, 1);

      return { ...result, score: combinedScore };
    });

    reranked.sort((a, b) => b.score - a.score);

    return topK ? reranked.slice(0, topK) : reranked;
  }

  // ---------- Storage Operations ----------

  async storeEmbedding(
    tenantId: string,
    knowledgeBaseId: string,
    documentId: string,
    content: string,
    embedding: number[],
    chunkIndex: number,
    metadata: Record<string, unknown>,
  ): Promise<string> {
    // Create the chunk without the embedding (Unsupported type in Prisma)
    const chunk = await this.prisma.documentChunk.create({
      data: {
        knowledgeBaseId,
        documentId,
        content,
        chunkIndex,
        metadata: metadata as any,
        tenantId,
      },
    });

    // Store the embedding via raw SQL (pgvector Unsupported type)
    const vectorStr = `[${embedding.join(',')}]`;
    await this.prisma.$executeRawUnsafe(
      `UPDATE "DocumentChunk" SET embedding = $1::vector WHERE id = $2`,
      vectorStr,
      chunk.id,
    );

    return chunk.id;
  }

  async deleteEmbeddingsByDocument(documentId: string): Promise<number> {
    const result = await this.prisma.documentChunk.deleteMany({
      where: { documentId },
    });
    return result.count;
  }

  async deleteEmbeddingsByKnowledgeBase(knowledgeBaseId: string): Promise<number> {
    const result = await this.prisma.documentChunk.deleteMany({
      where: { knowledgeBaseId },
    });
    return result.count;
  }

  // ---------- Helpers ----------

  private computeJaccardSimilarity(text1: string, text2: string): number {
    const tokens1 = new Set(text1.toLowerCase().split(/\s+/).filter((w) => w.length > 2));
    const tokens2 = new Set(text2.toLowerCase().split(/\s+/).filter((w) => w.length > 2));

    if (tokens1.size === 0 && tokens2.size === 0) return 1;
    if (tokens1.size === 0 || tokens2.size === 0) return 0;

    let intersection = 0;
    for (const token of tokens1) {
      if (tokens2.has(token)) intersection++;
    }

    const union = tokens1.size + tokens2.size - intersection;
    return union > 0 ? intersection / union : 0;
  }
}
