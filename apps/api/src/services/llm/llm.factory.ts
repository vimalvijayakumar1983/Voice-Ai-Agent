import { Injectable, BadRequestException } from '@nestjs/common';
import { ILLMProvider } from './llm.interface';
import { AnthropicProvider } from './providers/anthropic.provider';
import { OpenAIProvider } from './providers/openai.provider';

export type LLMProviderType = 'anthropic' | 'openai';

@Injectable()
export class LLMFactory {
  private readonly providers = new Map<string, ILLMProvider>();

  constructor(
    private readonly anthropicProvider: AnthropicProvider,
    private readonly openaiProvider: OpenAIProvider,
  ) {
    this.providers.set('anthropic', anthropicProvider);
    this.providers.set('openai', openaiProvider);
  }

  getProvider(type: LLMProviderType): ILLMProvider {
    const provider = this.providers.get(type);
    if (!provider) {
      throw new BadRequestException(`Unsupported LLM provider: ${type}`);
    }
    return provider;
  }

  getAvailableProviders(): string[] {
    return Array.from(this.providers.keys());
  }
}
