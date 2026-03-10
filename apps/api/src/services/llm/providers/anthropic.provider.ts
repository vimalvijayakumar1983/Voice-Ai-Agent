import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import Anthropic from '@anthropic-ai/sdk';
import {
  ILLMProvider,
  LLMMessage,
  LLMCompletionOptions,
  LLMCompletionResult,
  LLMFunctionDefinition,
  LLMFunctionCallResult,
} from '../llm.interface';

@Injectable()
export class AnthropicProvider implements ILLMProvider {
  readonly providerName = 'anthropic';
  private readonly logger = new Logger(AnthropicProvider.name);
  private readonly client: Anthropic;
  private readonly defaultModel: string;

  constructor(private readonly configService: ConfigService) {
    this.client = new Anthropic({
      apiKey: this.configService.get<string>('ANTHROPIC_API_KEY', ''),
    });
    this.defaultModel = this.configService.get<string>(
      'ANTHROPIC_MODEL',
      'claude-haiku-4-5-20251001',
    );
  }

  async complete(
    messages: LLMMessage[],
    options?: LLMCompletionOptions,
  ): Promise<LLMCompletionResult> {
    const systemMessage = messages.find((m) => m.role === 'system')?.content || '';
    const conversationMessages = messages
      .filter((m) => m.role !== 'system')
      .map((m) => ({
        role: m.role as 'user' | 'assistant',
        content: m.content,
      }));

    const response = await this.client.messages.create({
      model: options?.model || this.defaultModel,
      max_tokens: options?.maxTokens || 1024,
      system: systemMessage,
      messages: conversationMessages,
      temperature: options?.temperature,
      stop_sequences: options?.stopSequences,
    });

    const textContent = response.content.find((c) => c.type === 'text');

    return {
      content: textContent?.text || '',
      model: response.model,
      provider: this.providerName,
      usage: {
        promptTokens: response.usage.input_tokens,
        completionTokens: response.usage.output_tokens,
        totalTokens: response.usage.input_tokens + response.usage.output_tokens,
      },
      finishReason: response.stop_reason || 'end_turn',
    };
  }

  async completeStream(
    messages: LLMMessage[],
    options?: LLMCompletionOptions,
    onChunk?: (chunk: string) => void,
  ): Promise<LLMCompletionResult> {
    const systemMessage = messages.find((m) => m.role === 'system')?.content || '';
    const conversationMessages = messages
      .filter((m) => m.role !== 'system')
      .map((m) => ({
        role: m.role as 'user' | 'assistant',
        content: m.content,
      }));

    let fullContent = '';
    let inputTokens = 0;
    let outputTokens = 0;

    const stream = this.client.messages.stream({
      model: options?.model || this.defaultModel,
      max_tokens: options?.maxTokens || 1024,
      system: systemMessage,
      messages: conversationMessages,
      temperature: options?.temperature,
    });

    for await (const event of stream) {
      if (
        event.type === 'content_block_delta' &&
        event.delta.type === 'text_delta'
      ) {
        fullContent += event.delta.text;
        if (onChunk) onChunk(event.delta.text);
      }
    }

    const finalMessage = await stream.finalMessage();
    inputTokens = finalMessage.usage.input_tokens;
    outputTokens = finalMessage.usage.output_tokens;

    return {
      content: fullContent,
      model: finalMessage.model,
      provider: this.providerName,
      usage: {
        promptTokens: inputTokens,
        completionTokens: outputTokens,
        totalTokens: inputTokens + outputTokens,
      },
      finishReason: finalMessage.stop_reason || 'end_turn',
    };
  }

  async functionCall(
    messages: LLMMessage[],
    functions: LLMFunctionDefinition[],
    options?: LLMCompletionOptions,
  ): Promise<LLMFunctionCallResult> {
    const systemMessage = messages.find((m) => m.role === 'system')?.content || '';
    const conversationMessages = messages
      .filter((m) => m.role !== 'system')
      .map((m) => ({
        role: m.role as 'user' | 'assistant',
        content: m.content,
      }));

    const tools = functions.map((fn) => ({
      name: fn.name,
      description: fn.description,
      input_schema: fn.parameters as Anthropic.Tool.InputSchema,
    }));

    const response = await this.client.messages.create({
      model: options?.model || this.defaultModel,
      max_tokens: options?.maxTokens || 1024,
      system: systemMessage,
      messages: conversationMessages,
      tools,
      temperature: options?.temperature,
    });

    const toolUse = response.content.find((c) => c.type === 'tool_use');
    const textContent = response.content.find((c) => c.type === 'text');

    if (toolUse && toolUse.type === 'tool_use') {
      return {
        functionName: toolUse.name,
        arguments: toolUse.input as Record<string, unknown>,
        content: textContent?.type === 'text' ? textContent.text : undefined,
      };
    }

    return {
      functionName: '',
      arguments: {},
      content: textContent?.type === 'text' ? textContent.text : '',
    };
  }
}
