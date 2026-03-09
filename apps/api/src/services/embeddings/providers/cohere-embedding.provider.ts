import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import {
  IEmbeddingProvider,
  EmbeddingResult,
  BatchEmbeddingResult,
  EmbeddingOptions,
  EmbeddingInputType,
} from '../embedding.interface';

interface CohereEmbeddingResponse {
  id: string;
  texts: string[];
  embeddings: number[][];
  meta: {
    api_version: { version: string };
    billed_units: { input_tokens: number };
  };
}

const MODEL_DIMENSIONS: Record<string, number> = {
  'embed-english-v3.0': 1024,
  'embed-multilingual-v3.0': 1024,
  'embed-english-light-v3.0': 384,
  'embed-multilingual-light-v3.0': 384,
};

const INPUT_TYPE_MAP: Record<EmbeddingInputType, string> = {
  [EmbeddingInputType.SEARCH_DOCUMENT]: 'search_document',
  [EmbeddingInputType.SEARCH_QUERY]: 'search_query',
  [EmbeddingInputType.CLASSIFICATION]: 'classification',
  [EmbeddingInputType.CLUSTERING]: 'clustering',
};

@Injectable()
export class CohereEmbeddingProvider implements IEmbeddingProvider {
  readonly providerName = 'cohere';
  readonly defaultModel: string;
  readonly maxBatchSize = 96;

  private readonly logger = new Logger(CohereEmbeddingProvider.name);
  private readonly apiKey: string;
  private readonly baseUrl: string;

  constructor(private readonly configService: ConfigService) {
    this.apiKey = this.configService.get<string>('COHERE_API_KEY', '');
    this.defaultModel = this.configService.get<string>(
      'COHERE_EMBEDDING_MODEL',
      'embed-english-v3.0',
    );
    this.baseUrl = this.configService.get<string>(
      'COHERE_BASE_URL',
      'https://api.cohere.ai/v1',
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

    if (texts.length === 0) {
      return { embeddings: [], model, totalTokens: 0 };
    }

    if (texts.length > this.maxBatchSize) {
      throw new Error(
        `Batch size ${texts.length} exceeds Cohere maximum of ${this.maxBatchSize}`,
      );
    }

    const inputType = options?.inputType
      ? INPUT_TYPE_MAP[options.inputType]
      : 'search_document';

    this.logger.debug(
      `Cohere embedding: ${texts.length} texts, model=${model}, input_type=${inputType}`,
    );

    const body: Record<string, unknown> = {
      texts,
      model,
      input_type: inputType,
      truncate: 'END',
    };

    const response = await fetch(`${this.baseUrl}/embed`, {
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
        `Cohere embedding request failed: ${response.status} ${errorBody}`,
      );
      throw new Error(
        `Cohere embedding API error: ${response.status} ${response.statusText}`,
      );
    }

    const data = (await response.json()) as CohereEmbeddingResponse;
    const totalTokens = data.meta.billed_units.input_tokens;
    const tokensPerText = Math.round(totalTokens / texts.length);

    const embeddings: EmbeddingResult[] = data.embeddings.map(
      (vector) => ({
        vector,
        model,
        dimensions: vector.length,
        tokenCount: tokensPerText,
      }),
    );

    this.logger.debug(
      `Cohere generated ${embeddings.length} embeddings, total tokens: ${totalTokens}`,
    );

    return {
      embeddings,
      model,
      totalTokens,
    };
  }

  getModelDimensions(model?: string): number {
    const m = model || this.defaultModel;
    return MODEL_DIMENSIONS[m] || 1024;
  }
}
