import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { LLMFactory, LLMProviderType } from './llm.factory';
import { RedisService } from '../../common/services/redis.service';
import {
  LLMMessage,
  LLMCompletionOptions,
  LLMCompletionResult,
  LLMFunctionDefinition,
  LLMFunctionCallResult,
} from './llm.interface';
import * as crypto from 'crypto';

@Injectable()
export class LLMService {
  private readonly logger = new Logger(LLMService.name);
  private readonly defaultProvider: LLMProviderType;
  private readonly fallbackProvider: LLMProviderType;
  private readonly cacheEnabled: boolean;
  private readonly cacheTTL: number;

  constructor(
    private readonly factory: LLMFactory,
    private readonly configService: ConfigService,
    private readonly redisService: RedisService,
  ) {
    this.defaultProvider = this.configService.get<LLMProviderType>('LLM_PROVIDER', 'anthropic');
    this.fallbackProvider = this.configService.get<LLMProviderType>('LLM_FALLBACK_PROVIDER', 'openai');
    this.cacheEnabled = this.configService.get<boolean>('LLM_CACHE_ENABLED', false);
    this.cacheTTL = this.configService.get<number>('LLM_CACHE_TTL', 3600);
  }

  async complete(
    messages: LLMMessage[],
    options?: LLMCompletionOptions & { provider?: LLMProviderType },
  ): Promise<LLMCompletionResult> {
    const providerType = options?.provider || this.defaultProvider;

    // Check cache
    if (this.cacheEnabled) {
      const cacheKey = this.buildCacheKey(messages, options);
      const cached = await this.redisService.getJson<LLMCompletionResult>(cacheKey);
      if (cached) {
        this.logger.debug('LLM cache hit');
        return cached;
      }
    }

    try {
      const provider = this.factory.getProvider(providerType);
      const result = await provider.complete(messages, options);

      // Cache result
      if (this.cacheEnabled) {
        const cacheKey = this.buildCacheKey(messages, options);
        await this.redisService.setJson(cacheKey, result, this.cacheTTL);
      }

      return result;
    } catch (error) {
      this.logger.error(
        `LLM provider ${providerType} failed: ${(error as Error).message}`,
      );

      // Fallback to alternative provider
      if (providerType !== this.fallbackProvider) {
        this.logger.warn(`Falling back to ${this.fallbackProvider}`);
        const fallback = this.factory.getProvider(this.fallbackProvider);
        return fallback.complete(messages, options);
      }

      throw error;
    }
  }

  async completeStream(
    messages: LLMMessage[],
    options?: LLMCompletionOptions & { provider?: LLMProviderType },
    onChunk?: (chunk: string) => void,
  ): Promise<LLMCompletionResult> {
    const providerType = options?.provider || this.defaultProvider;

    try {
      const provider = this.factory.getProvider(providerType);
      return await provider.completeStream(messages, options, onChunk);
    } catch (error) {
      this.logger.error(
        `LLM stream provider ${providerType} failed: ${(error as Error).message}`,
      );

      if (providerType !== this.fallbackProvider) {
        this.logger.warn(`Stream falling back to ${this.fallbackProvider}`);
        const fallback = this.factory.getProvider(this.fallbackProvider);
        return fallback.completeStream(messages, options, onChunk);
      }

      throw error;
    }
  }

  async functionCall(
    messages: LLMMessage[],
    functions: LLMFunctionDefinition[],
    options?: LLMCompletionOptions & { provider?: LLMProviderType },
  ): Promise<LLMFunctionCallResult> {
    const providerType = options?.provider || this.defaultProvider;
    const provider = this.factory.getProvider(providerType);
    return provider.functionCall(messages, functions, options);
  }

  private buildCacheKey(messages: LLMMessage[], options?: LLMCompletionOptions): string {
    const hash = crypto
      .createHash('sha256')
      .update(JSON.stringify({ messages, options }))
      .digest('hex');
    return `llm:cache:${hash}`;
  }
}
