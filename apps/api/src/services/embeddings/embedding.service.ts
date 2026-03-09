import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { RedisService } from '../../common/services/redis.service';
import {
  IEmbeddingProvider,
  EmbeddingResult,
  BatchEmbeddingResult,
  EmbeddingOptions,
  EmbeddingProviderType,
} from './embedding.interface';
import { OpenAIEmbeddingProvider } from './providers/openai-embedding.provider';
import { CohereEmbeddingProvider } from './providers/cohere-embedding.provider';
import * as crypto from 'crypto';

@Injectable()
export class EmbeddingService {
  private readonly logger = new Logger(EmbeddingService.name);
  private readonly providers = new Map<string, IEmbeddingProvider>();
  private readonly defaultProvider: EmbeddingProviderType;
  private readonly fallbackProvider: EmbeddingProviderType;
  private readonly cacheEnabled: boolean;
  private readonly cacheTTL: number;
  private readonly batchChunkSize: number;

  constructor(
    private readonly configService: ConfigService,
    private readonly redisService: RedisService,
    openaiProvider: OpenAIEmbeddingProvider,
    cohereProvider: CohereEmbeddingProvider,
  ) {
    this.providers.set(EmbeddingProviderType.OPENAI, openaiProvider);
    this.providers.set(EmbeddingProviderType.COHERE, cohereProvider);

    this.defaultProvider = this.configService.get<EmbeddingProviderType>(
      'EMBEDDING_PROVIDER',
      EmbeddingProviderType.OPENAI,
    );
    this.fallbackProvider = this.configService.get<EmbeddingProviderType>(
      'EMBEDDING_FALLBACK_PROVIDER',
      EmbeddingProviderType.COHERE,
    );
    this.cacheEnabled = this.configService.get<boolean>(
      'EMBEDDING_CACHE_ENABLED',
      true,
    );
    this.cacheTTL = this.configService.get<number>(
      'EMBEDDING_CACHE_TTL',
      86400,
    );
    this.batchChunkSize = this.configService.get<number>(
      'EMBEDDING_BATCH_CHUNK_SIZE',
      100,
    );
  }

  private getProvider(type?: EmbeddingProviderType): IEmbeddingProvider {
    const providerType = type || this.defaultProvider;
    const provider = this.providers.get(providerType);
    if (!provider) {
      throw new Error(`Embedding provider '${providerType}' not found`);
    }
    return provider;
  }

  async embed(
    text: string,
    options?: EmbeddingOptions & { provider?: EmbeddingProviderType },
  ): Promise<EmbeddingResult> {
    const providerType = options?.provider || this.defaultProvider;

    if (this.cacheEnabled) {
      const cacheKey = this.buildCacheKey(text, options);
      const cached = await this.redisService.getJson<EmbeddingResult>(cacheKey);
      if (cached) {
        this.logger.debug('Embedding cache hit');
        return cached;
      }
    }

    try {
      const provider = this.getProvider(providerType);
      const result = await provider.embed(text, options);

      if (this.cacheEnabled) {
        const cacheKey = this.buildCacheKey(text, options);
        await this.redisService.setJson(cacheKey, result, this.cacheTTL);
      }

      return result;
    } catch (error) {
      this.logger.error(
        `Embedding provider ${providerType} failed: ${(error as Error).message}`,
      );

      if (providerType !== this.fallbackProvider) {
        this.logger.warn(`Falling back to ${this.fallbackProvider}`);
        const fallback = this.getProvider(this.fallbackProvider);
        return fallback.embed(text, options);
      }

      throw error;
    }
  }

  async embedBatch(
    texts: string[],
    options?: EmbeddingOptions & { provider?: EmbeddingProviderType },
  ): Promise<BatchEmbeddingResult> {
    const providerType = options?.provider || this.defaultProvider;
    const provider = this.getProvider(providerType);

    if (texts.length === 0) {
      return {
        embeddings: [],
        model: options?.model || provider.defaultModel,
        totalTokens: 0,
      };
    }

    // Check cache for individual texts
    const results: (EmbeddingResult | null)[] = new Array(texts.length).fill(null);
    const uncachedIndices: number[] = [];
    const uncachedTexts: string[] = [];

    if (this.cacheEnabled) {
      for (let i = 0; i < texts.length; i++) {
        const cacheKey = this.buildCacheKey(texts[i], options);
        const cached = await this.redisService.getJson<EmbeddingResult>(cacheKey);
        if (cached) {
          results[i] = cached;
        } else {
          uncachedIndices.push(i);
          uncachedTexts.push(texts[i]);
        }
      }

      this.logger.debug(
        `Embedding batch: ${texts.length - uncachedTexts.length} cache hits, ${uncachedTexts.length} to embed`,
      );

      if (uncachedTexts.length === 0) {
        return {
          embeddings: results as EmbeddingResult[],
          model: options?.model || provider.defaultModel,
          totalTokens: 0,
        };
      }
    } else {
      for (let i = 0; i < texts.length; i++) {
        uncachedIndices.push(i);
        uncachedTexts.push(texts[i]);
      }
    }

    // Process in chunks respecting provider max batch size
    const chunkSize = Math.min(this.batchChunkSize, provider.maxBatchSize);
    let totalTokens = 0;
    const allNewEmbeddings: EmbeddingResult[] = [];

    for (let start = 0; start < uncachedTexts.length; start += chunkSize) {
      const chunk = uncachedTexts.slice(start, start + chunkSize);

      try {
        const batchResult = await provider.embedBatch(chunk, options);
        totalTokens += batchResult.totalTokens;
        allNewEmbeddings.push(...batchResult.embeddings);
      } catch (error) {
        this.logger.error(
          `Batch embedding failed at chunk ${start}: ${(error as Error).message}`,
        );

        if (providerType !== this.fallbackProvider) {
          this.logger.warn(
            `Falling back to ${this.fallbackProvider} for remaining chunks`,
          );
          const fallback = this.getProvider(this.fallbackProvider);
          const fallbackResult = await fallback.embedBatch(chunk, options);
          totalTokens += fallbackResult.totalTokens;
          allNewEmbeddings.push(...fallbackResult.embeddings);
        } else {
          throw error;
        }
      }
    }

    // Merge cached and new results
    for (let i = 0; i < allNewEmbeddings.length; i++) {
      const originalIndex = uncachedIndices[i];
      results[originalIndex] = allNewEmbeddings[i];

      if (this.cacheEnabled) {
        const cacheKey = this.buildCacheKey(
          uncachedTexts[i],
          options,
        );
        await this.redisService.setJson(
          cacheKey,
          allNewEmbeddings[i],
          this.cacheTTL,
        );
      }
    }

    return {
      embeddings: results as EmbeddingResult[],
      model: options?.model || provider.defaultModel,
      totalTokens,
    };
  }

  getModelDimensions(
    model?: string,
    provider?: EmbeddingProviderType,
  ): number {
    return this.getProvider(provider).getModelDimensions(model);
  }

  private buildCacheKey(text: string, options?: EmbeddingOptions): string {
    const hash = crypto
      .createHash('sha256')
      .update(
        JSON.stringify({
          text,
          model: options?.model,
          dimensions: options?.dimensions,
          inputType: options?.inputType,
        }),
      )
      .digest('hex');
    return `embedding:cache:${hash}`;
  }
}
