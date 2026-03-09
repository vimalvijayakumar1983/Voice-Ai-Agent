import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import {
  IEmbeddingProvider,
  EmbeddingResult,
  BatchEmbeddingResult,
  EmbeddingOptions,
} from '../embedding.interface';

interface OpenAIEmbeddingResponse {
  object: string;
  data: Array<{
    object: string;
    index: number;
    embedding: number[];
  }>;
  model: string;
  usage: {
    prompt_tokens: number;
    total_tokens: number;
  };
}

const MODEL_DIMENSIONS: Record<string, number> = {
  'text-embedding-3-small': 1536,
  'text-embedding-3-large': 3072,
  'text-embedding-ada-002': 1536,
};

@Injectable()
export class OpenAIEmbeddingProvider implements IEmbeddingProvider {
  readonly providerName = 'openai';
  readonly defaultModel: string;
  readonly maxBatchSize = 2048;

  private readonly logger = new Logger(OpenAIEmbeddingProvider.name);
  private readonly apiKey: string;
  private readonly baseUrl: string;

  constructor(private readonly configService: ConfigService) {
    this.apiKey = this.configService.get<string>('OPENAI_API_KEY', '');
    this.defaultModel = this.configService.get<string>(
      'OPENAI_EMBEDDING_MODEL',
      'text-embedding-3-small',
    );
    this.baseUrl = this.configService.get<string>(
      'OPENAI_BASE_URL',
      'https://api.openai.com/v1',
    );
  }

  async embed(text: string, options?: EmbeddingOptions): Promise<EmbeddingResult> {
    const result = await this.embedBatch([text], options);
    return result.embeddings[0];
  }

  async embedBatch(
    texts: string[],
    options?: EmbeddingOptions,
  ): Promise<BatchEmbeddingResult> {
    const model = options?.model || this.defaultModel;
    const dimensions = options?.dimensions;

    if (texts.length === 0) {
      return { embeddings: [], model, totalTokens: 0 };
    }

    if (texts.length > this.maxBatchSize) {
      throw new Error(
        `Batch size ${texts.length} exceeds maximum of ${this.maxBatchSize}`,
      );
    }

    this.logger.debug(
      `Embedding ${texts.length} texts with model ${model}`,
    );

    const body: Record<string, unknown> = {
      input: texts,
      model,
    };

    if (dimensions && model !== 'text-embedding-ada-002') {
      body.dimensions = dimensions;
    }

    const response = await fetch(`${this.baseUrl}/embeddings`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${this.apiKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });

    if (!response.ok) {
      const errorBody = await response.text();
      this.logger.error(
        `OpenAI embedding request failed: ${response.status} ${errorBody}`,
      );
      throw new Error(
        `OpenAI embedding API error: ${response.status} ${response.statusText}`,
      );
    }

    const data = (await response.json()) as OpenAIEmbeddingResponse;

    const effectiveDimensions =
      dimensions || this.getModelDimensions(model);

    const embeddings: EmbeddingResult[] = data.data
      .sort((a, b) => a.index - b.index)
      .map((item) => ({
        vector: item.embedding,
        model: data.model,
        dimensions: item.embedding.length,
        tokenCount: Math.round(
          data.usage.prompt_tokens / texts.length,
        ),
      }));

    this.logger.debug(
      `Generated ${embeddings.length} embeddings, total tokens: ${data.usage.total_tokens}`,
    );

    return {
      embeddings,
      model: data.model,
      totalTokens: data.usage.total_tokens,
    };
  }

  getModelDimensions(model?: string): number {
    const m = model || this.defaultModel;
    return MODEL_DIMENSIONS[m] || 1536;
  }
}
