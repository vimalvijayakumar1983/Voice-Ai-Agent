export interface LLMMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

export interface LLMCompletionOptions {
  model?: string;
  temperature?: number;
  maxTokens?: number;
  stopSequences?: string[];
  metadata?: Record<string, unknown>;
}

export interface LLMCompletionResult {
  content: string;
  model: string;
  provider: string;
  usage: {
    promptTokens: number;
    completionTokens: number;
    totalTokens: number;
  };
  finishReason: string;
}

export interface LLMFunctionDefinition {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

export interface LLMFunctionCallResult {
  functionName: string;
  arguments: Record<string, unknown>;
  content?: string;
}

export interface ILLMProvider {
  readonly providerName: string;

  complete(
    messages: LLMMessage[],
    options?: LLMCompletionOptions,
  ): Promise<LLMCompletionResult>;

  completeStream(
    messages: LLMMessage[],
    options?: LLMCompletionOptions,
    onChunk?: (chunk: string) => void,
  ): Promise<LLMCompletionResult>;

  functionCall(
    messages: LLMMessage[],
    functions: LLMFunctionDefinition[],
    options?: LLMCompletionOptions,
  ): Promise<LLMFunctionCallResult>;
}
