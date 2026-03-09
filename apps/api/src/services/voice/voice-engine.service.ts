import { Injectable, Logger } from '@nestjs/common';
import { STTService, STTResult } from './stt.service';
import { TTSService } from './tts.service';
import { LLMService } from '../llm/llm.service';
import { LLMMessage } from '../llm/llm.interface';

export interface VoiceCallConfig {
  agentId: string;
  systemPrompt: string;
  voice: string;
  language: string;
  maxDurationSeconds: number;
  silenceTimeoutSeconds: number;
  temperature?: number;
  llmProvider?: string;
  llmModel?: string;
}

export interface VoiceCallSession {
  callId: string;
  config: VoiceCallConfig;
  conversationHistory: LLMMessage[];
  startTime: Date;
  isActive: boolean;
}

@Injectable()
export class VoiceEngineService {
  private readonly logger = new Logger(VoiceEngineService.name);
  private readonly activeSessions = new Map<string, VoiceCallSession>();

  constructor(
    private readonly sttService: STTService,
    private readonly ttsService: TTSService,
    private readonly llmService: LLMService,
  ) {}

  async startSession(callId: string, config: VoiceCallConfig): Promise<VoiceCallSession> {
    this.logger.log(`Starting voice session for call ${callId}`);

    const session: VoiceCallSession = {
      callId,
      config,
      conversationHistory: [
        { role: 'system', content: config.systemPrompt },
      ],
      startTime: new Date(),
      isActive: true,
    };

    this.activeSessions.set(callId, session);
    return session;
  }

  async processAudioInput(
    callId: string,
    audioChunk: Buffer,
    onResponseAudio?: (chunk: Buffer) => void,
  ): Promise<{ transcript: string; response: string }> {
    const session = this.activeSessions.get(callId);
    if (!session || !session.isActive) {
      throw new Error(`No active session for call ${callId}`);
    }

    // Step 1: Speech-to-Text
    const { Readable } = await import('stream');
    const audioStream = new Readable({
      read() {
        this.push(audioChunk);
        this.push(null);
      },
    });

    const sttResult: STTResult = await this.sttService.transcribe(audioStream, {
      language: session.config.language,
    });

    this.logger.debug(`STT result for ${callId}: "${sttResult.text}"`);

    // Step 2: Add user message to conversation
    session.conversationHistory.push({
      role: 'user',
      content: sttResult.text,
    });

    // Step 3: LLM completion
    const llmResult = await this.llmService.completeStream(
      session.conversationHistory,
      {
        temperature: session.config.temperature || 0.7,
        maxTokens: 256,
        provider: session.config.llmProvider as any,
        model: session.config.llmModel,
      },
      async (chunk: string) => {
        // Step 4: Stream TTS as LLM generates text (for low latency)
        if (onResponseAudio) {
          await this.ttsService.synthesizeStream(
            chunk,
            {
              voice: session.config.voice,
              language: session.config.language,
            },
            onResponseAudio,
          );
        }
      },
    );

    // Add assistant response to conversation history
    session.conversationHistory.push({
      role: 'assistant',
      content: llmResult.content,
    });

    return {
      transcript: sttResult.text,
      response: llmResult.content,
    };
  }

  async endSession(callId: string): Promise<{
    duration: number;
    turns: number;
    transcript: LLMMessage[];
  }> {
    const session = this.activeSessions.get(callId);
    if (!session) {
      throw new Error(`No session found for call ${callId}`);
    }

    session.isActive = false;
    const duration = Math.round(
      (Date.now() - session.startTime.getTime()) / 1000,
    );

    const result = {
      duration,
      turns: session.conversationHistory.filter((m) => m.role === 'user').length,
      transcript: session.conversationHistory,
    };

    this.activeSessions.delete(callId);
    this.logger.log(
      `Voice session ended for call ${callId}: ${duration}s, ${result.turns} turns`,
    );

    return result;
  }

  getActiveSessionCount(): number {
    return this.activeSessions.size;
  }

  isSessionActive(callId: string): boolean {
    return this.activeSessions.get(callId)?.isActive ?? false;
  }
}
