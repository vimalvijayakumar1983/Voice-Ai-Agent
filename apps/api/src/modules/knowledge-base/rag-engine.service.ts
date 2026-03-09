import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { RedisService } from '../../common/services/redis.service';
import { LLMService } from '../../services/llm/llm.service';
import { LLMMessage } from '../../services/llm/llm.interface';
import { VectorStoreService, VectorSearchResult } from './vector-store.service';

// ---------- Types ----------

export interface RAGQuery {
  query: string;
  knowledgeBaseId?: string;
  knowledgeBaseIds?: string[];
  conversationHistory?: ConversationMessage[];
  topK?: number;
  minConfidence?: number;
  searchStrategy?: 'vector' | 'hybrid' | 'mmr';
  includeSourceAttribution?: boolean;
  maxContextTokens?: number;
  systemPrompt?: string;
}

export interface ConversationMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface RAGResponse {
  answer: string;
  sources: RAGSource[];
  confidence: number;
  tokensUsed: {
    prompt: number;
    completion: number;
    total: number;
  };
  retrievedChunks: number;
  searchStrategy: string;
  cached: boolean;
}

export interface RAGSource {
  documentId: string;
  knowledgeBaseId: string;
  content: string;
  score: number;
  chunkIndex: number;
  metadata: Record<string, unknown>;
}

// ---------- Constants ----------

const DEFAULT_SYSTEM_PROMPT = `You are a helpful AI assistant that answers questions based on the provided context.
Follow these guidelines:
- Answer based ONLY on the provided context. Do not make up information.
- If the context does not contain enough information to answer the question, say so clearly.
- Cite your sources by referencing the document and section when possible.
- Be concise and accurate in your responses.
- If multiple sources provide conflicting information, mention the discrepancy.`;

const AVG_CHARS_PER_TOKEN = 4;

// ---------- Service ----------

@Injectable()
export class RAGEngineService {
  private readonly logger = new Logger(RAGEngineService.name);
  private readonly maxContextTokens: number;
  private readonly defaultTopK: number;
  private readonly defaultMinConfidence: number;
  private readonly queryCacheTTL: number;
  private readonly queryCacheEnabled: boolean;

  constructor(
    private readonly vectorStore: VectorStoreService,
    private readonly llmService: LLMService,
    private readonly redisService: RedisService,
    private readonly configService: ConfigService,
  ) {
    this.maxContextTokens = this.configService.get<number>('RAG_MAX_CONTEXT_TOKENS', 4000);
    this.defaultTopK = this.configService.get<number>('RAG_DEFAULT_TOP_K', 8);
    this.defaultMinConfidence = this.configService.get<number>('RAG_MIN_CONFIDENCE', 0.4);
    this.queryCacheTTL = this.configService.get<number>('RAG_QUERY_CACHE_TTL', 3600);
    this.queryCacheEnabled = this.configService.get<boolean>('RAG_QUERY_CACHE_ENABLED', true);
  }

  // ---------- Main RAG Pipeline ----------

  async query(tenantId: string, ragQuery: RAGQuery): Promise<RAGResponse> {
    const startTime = Date.now();

    // 1. Check query cache
    if (this.queryCacheEnabled) {
      const cacheKey = this.buildQueryCacheKey(tenantId, ragQuery);
      const cached = await this.redisService.getJson<RAGResponse>(cacheKey);
      if (cached) {
        this.logger.debug('RAG query cache hit');
        return { ...cached, cached: true };
      }
    }

    // 2. Reformulate query using conversation context
    const effectiveQuery = ragQuery.conversationHistory?.length
      ? await this.reformulateQuery(ragQuery.query, ragQuery.conversationHistory)
      : ragQuery.query;

    // 3. Retrieve relevant chunks
    const searchStrategy = ragQuery.searchStrategy || 'hybrid';
    const topK = ragQuery.topK || this.defaultTopK;

    const retrievedChunks = await this.retrieve(tenantId, effectiveQuery, {
      strategy: searchStrategy,
      topK,
      knowledgeBaseId: ragQuery.knowledgeBaseId,
      knowledgeBaseIds: ragQuery.knowledgeBaseIds,
      minConfidence: ragQuery.minConfidence ?? this.defaultMinConfidence,
    });

    // 4. Rerank results
    const reranked = this.vectorStore.rerank(retrievedChunks, effectiveQuery, topK);

    // 5. Build context within token budget
    const maxContextTokens = ragQuery.maxContextTokens || this.maxContextTokens;
    const contextChunks = this.fitChunksWithinTokenLimit(reranked, maxContextTokens);

    // 6. Calculate confidence score
    const confidence = this.calculateConfidence(contextChunks, effectiveQuery);

    // 7. Build augmented prompt
    const systemPrompt = ragQuery.systemPrompt || DEFAULT_SYSTEM_PROMPT;
    const messages = this.buildPromptMessages(
      systemPrompt,
      contextChunks,
      ragQuery.query,
      ragQuery.conversationHistory,
      ragQuery.includeSourceAttribution !== false,
    );

    // 8. Generate response via LLM
    const llmResult = await this.llmService.complete(messages);

    // 9. Build source attributions
    const sources: RAGSource[] = contextChunks.map((chunk) => ({
      documentId: chunk.documentId,
      knowledgeBaseId: chunk.knowledgeBaseId,
      content: chunk.content.substring(0, 200) + (chunk.content.length > 200 ? '...' : ''),
      score: chunk.score,
      chunkIndex: chunk.chunkIndex,
      metadata: chunk.metadata,
    }));

    const response: RAGResponse = {
      answer: llmResult.content,
      sources,
      confidence,
      tokensUsed: {
        prompt: llmResult.usage.promptTokens,
        completion: llmResult.usage.completionTokens,
        total: llmResult.usage.totalTokens,
      },
      retrievedChunks: contextChunks.length,
      searchStrategy,
      cached: false,
    };

    // 10. Cache the response
    if (this.queryCacheEnabled && confidence > 0.5) {
      const cacheKey = this.buildQueryCacheKey(tenantId, ragQuery);
      await this.redisService.setJson(cacheKey, response, this.queryCacheTTL);
    }

    this.logger.log(
      `RAG query completed in ${Date.now() - startTime}ms: ${contextChunks.length} chunks, confidence=${confidence.toFixed(2)}, strategy=${searchStrategy}`,
    );

    return response;
  }

  // ---------- Retrieval ----------

  private async retrieve(
    tenantId: string,
    query: string,
    options: {
      strategy: string;
      topK: number;
      knowledgeBaseId?: string;
      knowledgeBaseIds?: string[];
      minConfidence: number;
    },
  ): Promise<VectorSearchResult[]> {
    const searchOpts = {
      topK: options.topK,
      minScore: options.minConfidence,
      knowledgeBaseId: options.knowledgeBaseId,
    };

    // If multiple knowledge bases, search each and merge
    if (options.knowledgeBaseIds && options.knowledgeBaseIds.length > 0) {
      const allResults: VectorSearchResult[] = [];

      for (const kbId of options.knowledgeBaseIds) {
        const kbSearchOpts = { ...searchOpts, knowledgeBaseId: kbId };
        const results = await this.executeSearch(tenantId, query, options.strategy, kbSearchOpts);
        allResults.push(...results);
      }

      // Sort by score and deduplicate
      allResults.sort((a, b) => b.score - a.score);
      const seen = new Set<string>();
      return allResults.filter((r) => {
        if (seen.has(r.id)) return false;
        seen.add(r.id);
        return true;
      }).slice(0, options.topK);
    }

    return this.executeSearch(tenantId, query, options.strategy, searchOpts);
  }

  private async executeSearch(
    tenantId: string,
    query: string,
    strategy: string,
    options: { topK: number; minScore: number; knowledgeBaseId?: string },
  ): Promise<VectorSearchResult[]> {
    switch (strategy) {
      case 'hybrid':
        return this.vectorStore.hybridSearch(tenantId, query, options);
      case 'mmr':
        return this.vectorStore.mmrSearch(tenantId, query, {
          ...options,
          lambda: 0.5,
        });
      case 'vector':
      default:
        return this.vectorStore.searchByText(tenantId, query, options);
    }
  }

  // ---------- Context Window Management ----------

  private fitChunksWithinTokenLimit(
    chunks: VectorSearchResult[],
    maxTokens: number,
  ): VectorSearchResult[] {
    const result: VectorSearchResult[] = [];
    let currentTokens = 0;

    for (const chunk of chunks) {
      const chunkTokens = this.estimateTokens(chunk.content);

      if (currentTokens + chunkTokens > maxTokens) {
        // Try to fit a truncated version
        const remainingTokens = maxTokens - currentTokens;
        if (remainingTokens > 100) {
          const truncatedContent = chunk.content.substring(
            0,
            remainingTokens * AVG_CHARS_PER_TOKEN,
          );
          result.push({
            ...chunk,
            content: truncatedContent + '...',
          });
        }
        break;
      }

      result.push(chunk);
      currentTokens += chunkTokens;
    }

    return result;
  }

  private estimateTokens(text: string): number {
    return Math.ceil(text.length / AVG_CHARS_PER_TOKEN);
  }

  // ---------- Prompt Construction ----------

  private buildPromptMessages(
    systemPrompt: string,
    contextChunks: VectorSearchResult[],
    userQuery: string,
    conversationHistory?: ConversationMessage[],
    includeSourceAttribution?: boolean,
  ): LLMMessage[] {
    const messages: LLMMessage[] = [];

    // System message with context
    const contextText = contextChunks
      .map((chunk, i) => {
        const sourceRef = includeSourceAttribution
          ? `[Source ${i + 1}: Document ${chunk.documentId}, Chunk ${chunk.chunkIndex}]`
          : '';
        return `${sourceRef}\n${chunk.content}`;
      })
      .join('\n\n---\n\n');

    const fullSystemPrompt = `${systemPrompt}

## Context
The following information has been retrieved from the knowledge base to help answer the user's question:

${contextText}

## Instructions
- Base your answer on the context provided above.
- If citing information, reference the source number (e.g., [Source 1]).
- If the context is insufficient to answer, say so and explain what information is missing.`;

    messages.push({ role: 'system', content: fullSystemPrompt });

    // Add conversation history
    if (conversationHistory) {
      for (const msg of conversationHistory.slice(-6)) {
        messages.push({
          role: msg.role === 'user' ? 'user' : 'assistant',
          content: msg.content,
        });
      }
    }

    // Current user query
    messages.push({ role: 'user', content: userQuery });

    return messages;
  }

  // ---------- Conversation-Aware Query Reformulation ----------

  private async reformulateQuery(
    currentQuery: string,
    history: ConversationMessage[],
  ): Promise<string> {
    // Only reformulate if there's meaningful history
    if (history.length === 0) return currentQuery;

    const recentHistory = history.slice(-4);
    const historyContext = recentHistory
      .map((m) => `${m.role}: ${m.content}`)
      .join('\n');

    const reformulationPrompt: LLMMessage[] = [
      {
        role: 'system',
        content: `Given the conversation history and the latest user query, reformulate the query to be self-contained and specific.
The reformulated query should capture the full intent including any references to previous messages.
Return ONLY the reformulated query, nothing else.`,
      },
      {
        role: 'user',
        content: `Conversation history:\n${historyContext}\n\nLatest query: ${currentQuery}\n\nReformulated query:`,
      },
    ];

    try {
      const result = await this.llmService.complete(reformulationPrompt, {
        maxTokens: 200,
        temperature: 0,
      });
      const reformulated = result.content.trim();
      this.logger.debug(
        `Query reformulated: "${currentQuery}" -> "${reformulated}"`,
      );
      return reformulated;
    } catch (error) {
      this.logger.warn(
        `Query reformulation failed, using original: ${(error as Error).message}`,
      );
      return currentQuery;
    }
  }

  // ---------- Confidence Scoring ----------

  private calculateConfidence(
    chunks: VectorSearchResult[],
    query: string,
  ): number {
    if (chunks.length === 0) return 0;

    // Factor 1: Average similarity score of retrieved chunks
    const avgScore = chunks.reduce((sum, c) => sum + c.score, 0) / chunks.length;

    // Factor 2: Top score
    const topScore = chunks[0]?.score ?? 0;

    // Factor 3: Query term coverage in retrieved chunks
    const queryTerms = new Set(
      query
        .toLowerCase()
        .split(/\s+/)
        .filter((w) => w.length > 2),
    );
    const allChunkText = chunks.map((c) => c.content.toLowerCase()).join(' ');
    let coveredTerms = 0;
    for (const term of queryTerms) {
      if (allChunkText.includes(term)) coveredTerms++;
    }
    const termCoverage = queryTerms.size > 0 ? coveredTerms / queryTerms.size : 0;

    // Factor 4: Number of chunks found (more = more evidence)
    const chunkCountFactor = Math.min(chunks.length / 3, 1);

    // Weighted combination
    const confidence =
      0.35 * topScore +
      0.25 * avgScore +
      0.25 * termCoverage +
      0.15 * chunkCountFactor;

    return Math.max(0, Math.min(1, confidence));
  }

  // ---------- Cache ----------

  private buildQueryCacheKey(tenantId: string, ragQuery: RAGQuery): string {
    const queryHash = Buffer.from(
      JSON.stringify({
        query: ragQuery.query,
        kbId: ragQuery.knowledgeBaseId,
        kbIds: ragQuery.knowledgeBaseIds,
        strategy: ragQuery.searchStrategy,
        topK: ragQuery.topK,
      }),
    ).toString('base64url');

    return `rag:query:${tenantId}:${queryHash}`;
  }

  // ---------- Utility Methods ----------

  async invalidateCache(tenantId: string, knowledgeBaseId?: string): Promise<void> {
    const pattern = knowledgeBaseId
      ? `rag:query:${tenantId}:*`
      : `rag:query:${tenantId}:*`;

    const client = this.redisService.getClient();
    const keys = await client.keys(pattern);
    if (keys.length > 0) {
      await client.del(...keys);
      this.logger.log(`Invalidated ${keys.length} RAG query cache entries for tenant ${tenantId}`);
    }
  }
}
