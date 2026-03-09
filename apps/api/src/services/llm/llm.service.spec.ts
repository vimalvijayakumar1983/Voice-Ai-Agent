import { Test, TestingModule } from '@nestjs/testing';
import { ConfigService } from '@nestjs/config';
import { BadRequestException } from '@nestjs/common';
import { LlmService } from './llm.service';

// ─── Provider Mocks ─────────────────────────────────────────────────────────

const mockOpenAIProvider = {
  name: 'openai',
  generateCompletion: jest.fn(),
  generateStreamCompletion: jest.fn(),
  countTokens: jest.fn(),
};

const mockAnthropicProvider = {
  name: 'anthropic',
  generateCompletion: jest.fn(),
  generateStreamCompletion: jest.fn(),
  countTokens: jest.fn(),
};

const mockGroqProvider = {
  name: 'groq',
  generateCompletion: jest.fn(),
  generateStreamCompletion: jest.fn(),
  countTokens: jest.fn(),
};

const mockConfigService = {
  get: jest.fn((key: string) => {
    const config: Record<string, string> = {
      LLM_DEFAULT_PROVIDER: 'openai',
      OPENAI_API_KEY: 'sk-test-openai-key',
      ANTHROPIC_API_KEY: 'sk-test-anthropic-key',
      GROQ_API_KEY: 'gsk-test-groq-key',
    };
    return config[key];
  }),
};

// ─── Tests ──────────────────────────────────────────────────────────────────

describe('LlmService', () => {
  let service: LlmService;

  beforeEach(async () => {
    const module: TestingModule = await Test.createTestingModule({
      providers: [
        LlmService,
        { provide: ConfigService, useValue: mockConfigService },
        { provide: 'OpenAIProvider', useValue: mockOpenAIProvider },
        { provide: 'AnthropicProvider', useValue: mockAnthropicProvider },
        { provide: 'GroqProvider', useValue: mockGroqProvider },
      ],
    }).compile();

    service = module.get<LlmService>(LlmService);

    jest.clearAllMocks();
  });

  describe('generateResponse', () => {
    const baseRequest = {
      systemPrompt: 'You are a helpful medical receptionist.',
      messages: [
        { role: 'user' as const, content: 'I need to schedule an appointment.' },
      ],
      temperature: 0.7,
      maxTokens: 2048,
    };

    it('should generate a response using the default provider (OpenAI)', async () => {
      mockOpenAIProvider.generateCompletion.mockResolvedValue({
        content: 'I would be happy to help you schedule an appointment. What date works best for you?',
        model: 'gpt-4o',
        provider: 'openai',
        tokenUsage: { prompt: 45, completion: 22, total: 67 },
        finishReason: 'stop',
      });

      const result = await service.generateResponse(baseRequest);

      expect(result.content).toContain('schedule an appointment');
      expect(result.provider).toBe('openai');
      expect(result.tokenUsage.total).toBe(67);
      expect(mockOpenAIProvider.generateCompletion).toHaveBeenCalled();
    });

    it('should route to Anthropic provider when specified', async () => {
      mockAnthropicProvider.generateCompletion.mockResolvedValue({
        content: 'Certainly! I can help with scheduling. What type of appointment do you need?',
        model: 'claude-sonnet-4-20250514',
        provider: 'anthropic',
        tokenUsage: { prompt: 48, completion: 18, total: 66 },
        finishReason: 'end_turn',
      });

      const result = await service.generateResponse({
        ...baseRequest,
        provider: 'anthropic',
        model: 'claude-sonnet-4-20250514',
      });

      expect(result.provider).toBe('anthropic');
      expect(mockAnthropicProvider.generateCompletion).toHaveBeenCalled();
      expect(mockOpenAIProvider.generateCompletion).not.toHaveBeenCalled();
    });

    it('should route to Groq provider for fast inference', async () => {
      mockGroqProvider.generateCompletion.mockResolvedValue({
        content: 'Sure, let me check available slots for you.',
        model: 'llama-3.1-70b-versatile',
        provider: 'groq',
        tokenUsage: { prompt: 42, completion: 12, total: 54 },
        finishReason: 'stop',
      });

      const result = await service.generateResponse({
        ...baseRequest,
        provider: 'groq',
        model: 'llama-3.1-70b-versatile',
      });

      expect(result.provider).toBe('groq');
      expect(mockGroqProvider.generateCompletion).toHaveBeenCalled();
    });

    it('should throw BadRequestException for unsupported provider', async () => {
      await expect(
        service.generateResponse({ ...baseRequest, provider: 'unsupported' as any }),
      ).rejects.toThrow(BadRequestException);
    });

    it('should pass temperature and maxTokens to the provider', async () => {
      mockOpenAIProvider.generateCompletion.mockResolvedValue({
        content: 'Response',
        model: 'gpt-4o',
        provider: 'openai',
        tokenUsage: { prompt: 10, completion: 5, total: 15 },
        finishReason: 'stop',
      });

      await service.generateResponse({
        ...baseRequest,
        temperature: 0.3,
        maxTokens: 512,
      });

      expect(mockOpenAIProvider.generateCompletion).toHaveBeenCalledWith(
        expect.objectContaining({
          temperature: 0.3,
          maxTokens: 512,
        }),
      );
    });
  });

  describe('generateStreamResponse', () => {
    it('should return a stream from the provider', async () => {
      const mockStream = {
        [Symbol.asyncIterator]: async function* () {
          yield { content: 'Hello', done: false };
          yield { content: ' there!', done: false };
          yield { content: '', done: true };
        },
      };

      mockOpenAIProvider.generateStreamCompletion.mockResolvedValue(mockStream);

      const stream = await service.generateStreamResponse({
        systemPrompt: 'You are helpful.',
        messages: [{ role: 'user', content: 'Hi' }],
      });

      expect(stream).toBeDefined();
      expect(mockOpenAIProvider.generateStreamCompletion).toHaveBeenCalled();
    });
  });

  describe('countTokens', () => {
    it('should count tokens using the specified provider', async () => {
      mockOpenAIProvider.countTokens.mockResolvedValue(42);

      const count = await service.countTokens('This is a test message for token counting.', 'openai');

      expect(count).toBe(42);
      expect(mockOpenAIProvider.countTokens).toHaveBeenCalledWith(
        'This is a test message for token counting.',
      );
    });

    it('should count tokens using Anthropic provider', async () => {
      mockAnthropicProvider.countTokens.mockResolvedValue(38);

      const count = await service.countTokens('Sample text for Anthropic tokenizer.', 'anthropic');

      expect(count).toBe(38);
      expect(mockAnthropicProvider.countTokens).toHaveBeenCalled();
    });
  });

  describe('provider fallback', () => {
    it('should fallback to secondary provider on primary failure', async () => {
      mockOpenAIProvider.generateCompletion.mockRejectedValue(new Error('OpenAI rate limit exceeded'));
      mockAnthropicProvider.generateCompletion.mockResolvedValue({
        content: 'Fallback response from Anthropic.',
        model: 'claude-sonnet-4-20250514',
        provider: 'anthropic',
        tokenUsage: { prompt: 40, completion: 10, total: 50 },
        finishReason: 'end_turn',
      });

      const result = await service.generateResponse({
        systemPrompt: 'You are helpful.',
        messages: [{ role: 'user', content: 'Hello' }],
        fallbackProvider: 'anthropic',
      });

      expect(result.provider).toBe('anthropic');
      expect(result.content).toBe('Fallback response from Anthropic.');
    });
  });
});
