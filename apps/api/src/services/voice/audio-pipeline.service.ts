import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { EventEmitter } from 'events';
import { RedisService } from '../../common/services/redis.service';
import { DeepgramSTTProvider, DeepgramStreamSession, DeepgramTranscript } from './providers/deepgram-stt.provider';
import { ElevenLabsTTSProvider, ElevenLabsTTSSession } from './providers/elevenlabs-tts.provider';
import { LLMService } from '../llm/llm.service';
import { LLMMessage } from '../llm/llm.interface';

/**
 * Pipeline states for a call audio session.
 */
export enum PipelineState {
  IDLE = 'idle',
  LISTENING = 'listening',
  PROCESSING = 'processing',
  SPEAKING = 'speaking',
  INTERRUPTED = 'interrupted',
}

export interface AudioPipelineConfig {
  callId: string;
  tenantId: string;
  agentId: string;
  systemPrompt: string;
  voice: string;
  language: string;
  sttProvider?: 'deepgram' | 'google';
  ttsProvider?: 'elevenlabs' | 'azure';
  llmProvider?: string;
  llmModel?: string;
  silenceTimeoutMs?: number;
  maxCallDurationMs?: number;
  bargeInEnabled?: boolean;
  temperature?: number;
  sampleRate?: number;
  encoding?: 'linear16' | 'mulaw';
}

export interface PipelineMetrics {
  sttLatencyMs: number;
  llmLatencyMs: number;
  ttsLatencyMs: number;
  totalLatencyMs: number;
  turnCount: number;
  totalAudioReceivedBytes: number;
  totalAudioSentBytes: number;
  interruptionCount: number;
}

interface PipelineSession {
  config: AudioPipelineConfig;
  state: PipelineState;
  conversationHistory: LLMMessage[];
  sttSession: DeepgramStreamSession | null;
  ttsSession: ElevenLabsTTSSession | null;
  metrics: PipelineMetrics;
  currentTranscript: string;
  currentInterimTranscript: string;
  lastSpeechTimestamp: number;
  silenceTimer: ReturnType<typeof setTimeout> | null;
  callStartTime: number;
  callDurationTimer: ReturnType<typeof setTimeout> | null;
  isProcessingLLM: boolean;
  pendingTTSChunks: Buffer[];
  streamSid: string | null;
}

@Injectable()
export class AudioPipelineService extends EventEmitter {
  private readonly logger = new Logger(AudioPipelineService.name);
  private readonly sessions = new Map<string, PipelineSession>();
  private readonly defaultSilenceTimeoutMs: number;
  private readonly defaultMaxCallDurationMs: number;

  constructor(
    private readonly configService: ConfigService,
    private readonly redisService: RedisService,
    private readonly deepgramSTT: DeepgramSTTProvider,
    private readonly elevenLabsTTS: ElevenLabsTTSProvider,
    private readonly llmService: LLMService,
  ) {
    super();
    this.defaultSilenceTimeoutMs = this.configService.get<number>('PIPELINE_SILENCE_TIMEOUT_MS', 2000);
    this.defaultMaxCallDurationMs = this.configService.get<number>('PIPELINE_MAX_CALL_DURATION_MS', 1800000);
  }

  /**
   * Starts a new audio pipeline session for a call.
   * Initializes STT and conversation state.
   */
  async startSession(config: AudioPipelineConfig): Promise<void> {
    const { callId } = config;

    if (this.sessions.has(callId)) {
      this.logger.warn(`Session ${callId} already exists, ending existing session`);
      await this.endSession(callId);
    }

    this.logger.log(`Starting audio pipeline for call ${callId}`);

    const session: PipelineSession = {
      config,
      state: PipelineState.IDLE,
      conversationHistory: [
        { role: 'system', content: config.systemPrompt },
      ],
      sttSession: null,
      ttsSession: null,
      metrics: {
        sttLatencyMs: 0,
        llmLatencyMs: 0,
        ttsLatencyMs: 0,
        totalLatencyMs: 0,
        turnCount: 0,
        totalAudioReceivedBytes: 0,
        totalAudioSentBytes: 0,
        interruptionCount: 0,
      },
      currentTranscript: '',
      currentInterimTranscript: '',
      lastSpeechTimestamp: 0,
      silenceTimer: null,
      callStartTime: Date.now(),
      callDurationTimer: null,
      isProcessingLLM: false,
      pendingTTSChunks: [],
      streamSid: null,
    };

    this.sessions.set(callId, session);

    // Set up STT streaming session
    const sttSession = this.deepgramSTT.createStream(callId, {
      language: config.language || 'en',
      encoding: config.encoding || 'linear16',
      sampleRate: config.sampleRate || 16000,
      interimResults: true,
      punctuate: true,
      vadEvents: true,
      endpointing: 300,
      utteranceEndMs: 1000,
      smartFormat: true,
    });

    session.sttSession = sttSession;

    // Handle STT transcripts
    sttSession.on('transcript', (transcript: DeepgramTranscript) => {
      this.handleTranscript(callId, transcript);
    });

    sttSession.on('vad', (data: { isSpeaking: boolean }) => {
      this.handleVAD(callId, data.isSpeaking);
    });

    sttSession.on('utteranceEnd', () => {
      this.handleUtteranceEnd(callId);
    });

    sttSession.on('error', (error: Error) => {
      this.logger.error(`STT error for call ${callId}: ${error.message}`);
      this.emit('pipeline:error', { callId, error: error.message, stage: 'stt' });
    });

    // Set max call duration timer
    const maxDuration = config.maxCallDurationMs || this.defaultMaxCallDurationMs;
    session.callDurationTimer = setTimeout(() => {
      this.logger.log(`Max call duration reached for ${callId}`);
      this.emit('pipeline:maxDuration', { callId });
      this.endSession(callId);
    }, maxDuration);

    // Set initial state to listening
    this.updateState(callId, PipelineState.LISTENING);

    // Store session metadata in Redis
    await this.redisService.setJson(`pipeline:session:${callId}`, {
      callId: config.callId,
      tenantId: config.tenantId,
      agentId: config.agentId,
      state: PipelineState.LISTENING,
      startedAt: new Date().toISOString(),
    }, 7200);

    this.emit('pipeline:started', { callId, config });
  }

  /**
   * Feeds incoming audio data from the telephony provider into the STT pipeline.
   */
  processIncomingAudio(callId: string, pcmAudio: Buffer): void {
    const session = this.sessions.get(callId);
    if (!session) return;

    session.metrics.totalAudioReceivedBytes += pcmAudio.length;

    // Handle barge-in: if user speaks while TTS is playing, interrupt the TTS
    if (session.state === PipelineState.SPEAKING && session.config.bargeInEnabled !== false) {
      const energy = this.calculateAudioEnergy(pcmAudio);
      if (energy > 500) { // Threshold for voice activity
        this.handleBargeIn(callId);
      }
    }

    // Send audio to STT
    if (session.sttSession) {
      session.sttSession.sendAudio(pcmAudio);
    }
  }

  /**
   * Sets the Twilio stream SID for sending audio back.
   */
  setStreamSid(callId: string, streamSid: string): void {
    const session = this.sessions.get(callId);
    if (session) {
      session.streamSid = streamSid;
    }
  }

  /**
   * Ends a pipeline session and cleans up all resources.
   */
  async endSession(callId: string): Promise<{
    metrics: PipelineMetrics;
    conversationHistory: LLMMessage[];
    duration: number;
  } | null> {
    const session = this.sessions.get(callId);
    if (!session) {
      this.logger.warn(`No pipeline session found for call ${callId}`);
      return null;
    }

    this.logger.log(`Ending audio pipeline for call ${callId}`);

    // Clear timers
    if (session.silenceTimer) {
      clearTimeout(session.silenceTimer);
    }
    if (session.callDurationTimer) {
      clearTimeout(session.callDurationTimer);
    }

    // Close STT session
    if (session.sttSession) {
      session.sttSession.close();
    }

    // Close TTS session
    if (session.ttsSession) {
      session.ttsSession.close();
    }

    const duration = Math.round((Date.now() - session.callStartTime) / 1000);
    const result = {
      metrics: { ...session.metrics },
      conversationHistory: [...session.conversationHistory],
      duration,
    };

    // Clean up Redis
    await this.redisService.del(`pipeline:session:${callId}`);

    this.sessions.delete(callId);
    this.updateState(callId, PipelineState.IDLE);

    this.emit('pipeline:ended', { callId, ...result });
    return result;
  }

  /**
   * Gets the current state and metrics for a call.
   */
  getSessionInfo(callId: string): {
    state: PipelineState;
    metrics: PipelineMetrics;
    turnCount: number;
  } | null {
    const session = this.sessions.get(callId);
    if (!session) return null;

    return {
      state: session.state,
      metrics: { ...session.metrics },
      turnCount: session.conversationHistory.filter((m) => m.role === 'user').length,
    };
  }

  /**
   * Returns count of active pipeline sessions.
   */
  getActiveSessionCount(): number {
    return this.sessions.size;
  }

  // ─── PRIVATE HANDLERS ─────────────────────────────────────────────────────────

  private handleTranscript(callId: string, transcript: DeepgramTranscript): void {
    const session = this.sessions.get(callId);
    if (!session) return;

    if (transcript.isFinal) {
      // Accumulate final transcripts for the current utterance
      session.currentTranscript += (session.currentTranscript ? ' ' : '') + transcript.text;
      session.currentInterimTranscript = '';

      this.emit('pipeline:transcript', {
        callId,
        text: transcript.text,
        isFinal: true,
        confidence: transcript.confidence,
        speaker: 'human',
      });
    } else {
      // Update interim transcript for real-time display
      session.currentInterimTranscript = transcript.text;

      this.emit('pipeline:transcript', {
        callId,
        text: transcript.text,
        isFinal: false,
        confidence: transcript.confidence,
        speaker: 'human',
      });
    }

    session.lastSpeechTimestamp = Date.now();
    this.resetSilenceTimer(callId);
  }

  private handleVAD(callId: string, isSpeaking: boolean): void {
    const session = this.sessions.get(callId);
    if (!session) return;

    if (isSpeaking) {
      if (session.state !== PipelineState.LISTENING && session.state !== PipelineState.INTERRUPTED) {
        this.updateState(callId, PipelineState.LISTENING);
      }
    }

    this.emit('pipeline:vad', { callId, isSpeaking });
  }

  private handleUtteranceEnd(callId: string): void {
    const session = this.sessions.get(callId);
    if (!session) return;

    // When an utterance ends and we have accumulated transcript, process it
    if (session.currentTranscript.trim().length > 0) {
      this.processCompletedUtterance(callId);
    }
  }

  private async processCompletedUtterance(callId: string): Promise<void> {
    const session = this.sessions.get(callId);
    if (!session || session.isProcessingLLM) return;

    const userText = session.currentTranscript.trim();
    if (!userText) return;

    session.isProcessingLLM = true;
    session.currentTranscript = '';
    session.currentInterimTranscript = '';

    this.updateState(callId, PipelineState.PROCESSING);

    // Add user message to conversation history
    session.conversationHistory.push({
      role: 'user',
      content: userText,
    });

    session.metrics.turnCount++;

    this.emit('pipeline:userMessage', { callId, text: userText });

    const llmStartTime = Date.now();
    let llmFirstChunkTime = 0;
    let fullResponse = '';

    try {
      // Create TTS session for streaming response audio
      const ttsSession = this.elevenLabsTTS.createStream(`tts-${callId}-${Date.now()}`, {
        voiceId: session.config.voice,
        outputFormat: 'pcm_16000',
        optimizeStreamingLatency: 3,
      });

      session.ttsSession = ttsSession;

      // Collect TTS audio chunks
      ttsSession.on('audio', (data: { audio: Buffer; isFinal: boolean }) => {
        if (data.audio.length > 0) {
          session.metrics.totalAudioSentBytes += data.audio.length;
          this.emit('pipeline:audioOut', { callId, audio: data.audio, isFinal: data.isFinal });
        }
      });

      // Stream LLM response with sentence-level TTS
      let sentenceBuffer = '';
      const ttsStartTime = Date.now();

      const llmResult = await this.llmService.completeStream(
        session.conversationHistory,
        {
          temperature: session.config.temperature || 0.7,
          maxTokens: 512,
          provider: session.config.llmProvider as any,
          model: session.config.llmModel,
        },
        (chunk: string) => {
          if (!llmFirstChunkTime) {
            llmFirstChunkTime = Date.now();
            session.metrics.llmLatencyMs = llmFirstChunkTime - llmStartTime;
            this.updateState(callId, PipelineState.SPEAKING);
          }

          fullResponse += chunk;
          sentenceBuffer += chunk;

          // Send complete sentences to TTS for streaming synthesis
          const sentenceEnd = sentenceBuffer.search(/[.!?]\s/);
          if (sentenceEnd !== -1) {
            const sentence = sentenceBuffer.slice(0, sentenceEnd + 1).trim();
            sentenceBuffer = sentenceBuffer.slice(sentenceEnd + 2);

            if (sentence && session.state === PipelineState.SPEAKING) {
              ttsSession.sendText(sentence + ' ');
            }
          }
        },
      );

      // Flush remaining text to TTS
      const remainingText = sentenceBuffer.trim();
      if (remainingText && session.state === PipelineState.SPEAKING) {
        ttsSession.sendText(remainingText);
      }
      ttsSession.flush();

      // Record metrics
      const ttsEndTime = Date.now();
      session.metrics.ttsLatencyMs = ttsStartTime > 0 ? ttsEndTime - ttsStartTime : 0;
      session.metrics.totalLatencyMs = ttsEndTime - llmStartTime;
      session.metrics.sttLatencyMs = llmStartTime - session.lastSpeechTimestamp;

      // Add assistant response to history
      session.conversationHistory.push({
        role: 'assistant',
        content: llmResult.content || fullResponse,
      });

      this.emit('pipeline:assistantMessage', {
        callId,
        text: llmResult.content || fullResponse,
      });

      // Update metrics in Redis
      await this.redisService.setJson(`pipeline:metrics:${callId}`, session.metrics, 7200);

    } catch (error) {
      this.logger.error(`LLM processing error for call ${callId}: ${(error as Error).message}`);
      this.emit('pipeline:error', { callId, error: (error as Error).message, stage: 'llm' });
    } finally {
      session.isProcessingLLM = false;
      if (session.state === PipelineState.SPEAKING || session.state === PipelineState.PROCESSING) {
        this.updateState(callId, PipelineState.LISTENING);
      }
    }
  }

  private handleBargeIn(callId: string): void {
    const session = this.sessions.get(callId);
    if (!session) return;

    this.logger.log(`Barge-in detected for call ${callId}`);
    session.metrics.interruptionCount++;

    // Close current TTS session to stop playback
    if (session.ttsSession) {
      session.ttsSession.close();
      session.ttsSession = null;
    }

    // Clear any pending TTS chunks
    session.pendingTTSChunks = [];

    this.updateState(callId, PipelineState.INTERRUPTED);

    // Emit clear event for telephony provider to stop playing audio
    this.emit('pipeline:bargeIn', { callId });

    // Reset to listening state
    this.updateState(callId, PipelineState.LISTENING);
  }

  private resetSilenceTimer(callId: string): void {
    const session = this.sessions.get(callId);
    if (!session) return;

    if (session.silenceTimer) {
      clearTimeout(session.silenceTimer);
    }

    const silenceTimeout = session.config.silenceTimeoutMs || this.defaultSilenceTimeoutMs;

    session.silenceTimer = setTimeout(() => {
      if (session.state === PipelineState.LISTENING) {
        // If we have accumulated transcript, process it
        if (session.currentTranscript.trim().length > 0) {
          this.processCompletedUtterance(callId);
        } else {
          this.emit('pipeline:silence', { callId, durationMs: silenceTimeout });
        }
      }
    }, silenceTimeout);
  }

  private updateState(callId: string, newState: PipelineState): void {
    const session = this.sessions.get(callId);
    if (session) {
      const previousState = session.state;
      session.state = newState;
      this.emit('pipeline:stateChange', { callId, previousState, newState });
    }
  }

  /**
   * Calculates the RMS energy of an audio buffer for voice activity detection.
   */
  private calculateAudioEnergy(pcmBuffer: Buffer): number {
    if (pcmBuffer.length < 2) return 0;

    let sumSquares = 0;
    const sampleCount = Math.floor(pcmBuffer.length / 2);

    for (let i = 0; i < sampleCount; i++) {
      const sample = pcmBuffer.readInt16LE(i * 2);
      sumSquares += sample * sample;
    }

    return Math.sqrt(sumSquares / sampleCount);
  }
}
