export interface EmbeddingResult {
  vector: number[];
  model: string;
  dimensions: number;
  tokenCount: number;
}

export interface BatchEmbeddingResult {
  embeddings: EmbeddingResult[];
  model: string;
  totalTokens: number;
}

export interface EmbeddingOptions {
  model?: string;
  dimensions?: number;
  inputType?: EmbeddingInputType;
}

export enum EmbeddingInputType {
  SEARCH_DOCUMENT = 'search_document',
  SEARCH_QUERY = 'search_query',
  CLASSIFICATION = 'classification',
  CLUSTERING = 'clustering',
}

export enum EmbeddingProviderType {
  OPENAI = 'openai',
  COHERE = 'cohere',
}

export interface IEmbeddingProvider {
  readonly providerName: string;
  readonly defaultModel: string;
  readonly maxBatchSize: number;

  embed(text: string, options?: EmbeddingOptions): Promise<EmbeddingResult>;

  embedBatch(
    texts: string[],
    options?: EmbeddingOptions,
  ): Promise<BatchEmbeddingResult>;

  getModelDimensions(model?: string): number;
}
