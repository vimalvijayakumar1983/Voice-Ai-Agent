import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { EventEmitter } from 'events';
import WebSocket from 'ws';

export interface DeepgramTranscript {
  text: string;
  confidence: number;
  isFinal: boolean;
  words: DeepgramWord[];
  language: string;
  durationMs: number;
  speechFinal: boolean;
  startTime: number;
  endTime: number;
}

export interface DeepgramWord {
  word: string;
  start: number;
  end: number;
  confidence: number;
  punctuatedWord: string;
  speaker?: number;
}

export interface DeepgramStreamOptions {
  language?: string;
  model?: string;
  sampleRate?: number;
  encoding?: 'linear16' | 'mulaw' | 'flac' | 'opus';
  channels?: number;
  interimResults?: boolean;
  punctuate?: boolean;
  vadEvents?: boolean;
  endpointing?: number | false;
  utteranceEndMs?: number;
  smartFormat?: boolean;
  diarize?: boolean;
  keywords?: string[];
  detectLanguage?: boolean;
}

interface DeepgramResponse {
  type: string;
  channel_index?: number[];
  duration?: number;
  start?: number;
  is_final?: boolean;
  speech_final?: boolean;
  channel?: {
    alternatives: Array<{
      transcript: string;
      confidence: number;
      words: Array<{
        word: string;
        start: number;
        end: number;
        confidence: number;
        punctuated_word: string;
        speaker?: number;
      }>;
    }>;
  };
  metadata?: {
    request_id: string;
    model_info: { name: string; version: string };
  };
  from_finalize?: boolean;
}

@Injectable()
export class DeepgramSTTProvider extends EventEmitter {
  private readonly logger = new Logger(DeepgramSTTProvider.name);
  private readonly apiKey: string;
  private readonly baseUrl: string;
  private activeSessions = new Map<string, DeepgramStreamSession>();

  constructor(private readonly configService: ConfigService) {
    super();
    this.apiKey = this.configService.get<string>('DEEPGRAM_API_KEY', '');
    this.baseUrl = this.configService.get<string>(
      'DEEPGRAM_WS_URL',
      'wss://api.deepgram.com/v1/listen',
    );
  }

  /**
   * Creates a real-time STT streaming session with Deepgram.
   * Returns a session object that accepts audio data and emits transcript events.
   */
  createStream(
    sessionId: string,
    options: DeepgramStreamOptions = {},
  ): DeepgramStreamSession {
    const existingSession = this.activeSessions.get(sessionId);
    if (existingSession && existingSession.isActive) {
      this.logger.warn(`Session ${sessionId} already exists, closing existing`);
      existingSession.close();
    }

    const session = new DeepgramStreamSession(
      sessionId,
      this.apiKey,
      this.baseUrl,
      options,
      this.logger,
    );

    session.on('transcript', (transcript: DeepgramTranscript) => {
      this.emit('transcript', { sessionId, ...transcript });
    });

    session.on('vad', (data: { sessionId: string; isSpeaking: boolean }) => {
      this.emit('vad', { sessionId, ...data });
    });

    session.on('error', (error: Error) => {
      this.emit('error', { sessionId, error });
    });

    session.on('close', () => {
      this.activeSessions.delete(sessionId);
      this.emit('close', { sessionId });
    });

    this.activeSessions.set(sessionId, session);
    return session;
  }

  /**
   * Sends audio data to an existing stream session.
   */
  sendAudio(sessionId: string, audioData: Buffer): void {
    const session = this.activeSessions.get(sessionId);
    if (!session || !session.isActive) {
      this.logger.warn(`No active session ${sessionId} to send audio to`);
      return;
    }
    session.sendAudio(audioData);
  }

  /**
   * Closes a stream session and triggers final transcript.
   */
  closeStream(sessionId: string): void {
    const session = this.activeSessions.get(sessionId);
    if (session) {
      session.close();
      this.activeSessions.delete(sessionId);
    }
  }

  /**
   * Returns count of active streaming sessions.
   */
  getActiveSessionCount(): number {
    return this.activeSessions.size;
  }

  /**
   * Checks if a specific session is active.
   */
  isSessionActive(sessionId: string): boolean {
    return this.activeSessions.get(sessionId)?.isActive ?? false;
  }
}

export class DeepgramStreamSession extends EventEmitter {
  private ws: WebSocket | null = null;
  private _isActive = false;
  private reconnectAttempts = 0;
  private readonly maxReconnectAttempts = 3;
  private keepAliveInterval: ReturnType<typeof setInterval> | null = null;
  private audioBuffer: Buffer[] = [];
  private readonly maxBufferSize = 100;

  get isActive(): boolean {
    return this._isActive;
  }

  constructor(
    public readonly sessionId: string,
    private readonly apiKey: string,
    private readonly baseUrl: string,
    private readonly options: DeepgramStreamOptions,
    private readonly logger: Logger,
  ) {
    super();
    this.connect();
  }

  /**
   * Sends audio data to Deepgram for transcription.
   */
  sendAudio(audioData: Buffer): void {
    if (!this._isActive || !this.ws || this.ws.readyState !== WebSocket.OPEN) {
      // Buffer audio if connection is not yet ready
      if (this.audioBuffer.length < this.maxBufferSize) {
        this.audioBuffer.push(audioData);
      }
      return;
    }

    try {
      this.ws.send(audioData);
    } catch (error) {
      this.logger.error(`Failed to send audio for session ${this.sessionId}: ${(error as Error).message}`);
    }
  }

  /**
   * Closes the Deepgram connection gracefully.
   * Sends a finalize message to trigger any remaining transcript.
   */
  close(): void {
    this._isActive = false;

    if (this.keepAliveInterval) {
      clearInterval(this.keepAliveInterval);
      this.keepAliveInterval = null;
    }

    if (this.ws) {
      try {
        // Send close message to trigger final transcript
        if (this.ws.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({ type: 'CloseStream' }));
        }
        this.ws.close();
      } catch (error) {
        this.logger.debug(`Error closing Deepgram WebSocket: ${(error as Error).message}`);
      }
      this.ws = null;
    }

    this.audioBuffer = [];
    this.emit('close');
  }

  private connect(): void {
    const queryParams = this.buildQueryParams();
    const url = `${this.baseUrl}?${queryParams}`;

    this.logger.log(`Connecting Deepgram STT for session ${this.sessionId}`);

    try {
      this.ws = new WebSocket(url, {
        headers: {
          Authorization: `Token ${this.apiKey}`,
        },
      });
    } catch (error) {
      this.logger.error(`Failed to create Deepgram WebSocket: ${(error as Error).message}`);
      this.emit('error', error);
      return;
    }

    this.ws.on('open', () => {
      this._isActive = true;
      this.reconnectAttempts = 0;
      this.logger.log(`Deepgram STT connected for session ${this.sessionId}`);

      // Flush buffered audio
      while (this.audioBuffer.length > 0) {
        const buffered = this.audioBuffer.shift();
        if (buffered && this.ws?.readyState === WebSocket.OPEN) {
          this.ws.send(buffered);
        }
      }

      // Start keep-alive pings every 10 seconds
      this.keepAliveInterval = setInterval(() => {
        if (this.ws?.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({ type: 'KeepAlive' }));
        }
      }, 10000);
    });

    this.ws.on('message', (data: WebSocket.Data) => {
      try {
        const message: DeepgramResponse = JSON.parse(data.toString());
        this.handleMessage(message);
      } catch (error) {
        this.logger.error(`Failed to parse Deepgram message: ${(error as Error).message}`);
      }
    });

    this.ws.on('error', (error: Error) => {
      this.logger.error(`Deepgram WebSocket error for session ${this.sessionId}: ${error.message}`);
      this.emit('error', error);
    });

    this.ws.on('close', (code: number, reason: Buffer) => {
      this.logger.log(`Deepgram WebSocket closed for session ${this.sessionId}: code=${code}`);

      if (this.keepAliveInterval) {
        clearInterval(this.keepAliveInterval);
        this.keepAliveInterval = null;
      }

      // Attempt reconnection if not intentionally closed
      if (this._isActive && this.reconnectAttempts < this.maxReconnectAttempts) {
        this.reconnectAttempts++;
        const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), 10000);
        this.logger.log(`Reconnecting Deepgram in ${delay}ms (attempt ${this.reconnectAttempts})`);
        setTimeout(() => this.connect(), delay);
      } else {
        this._isActive = false;
        this.emit('close');
      }
    });
  }

  private handleMessage(message: DeepgramResponse): void {
    switch (message.type) {
      case 'Results': {
        if (!message.channel?.alternatives?.[0]) return;

        const alt = message.channel.alternatives[0];
        const transcript = alt.transcript;

        // Skip empty transcripts
        if (!transcript || transcript.trim().length === 0) {
          // If speech_final is true with empty text, it indicates VAD end-of-speech
          if (message.speech_final) {
            this.emit('vad', { isSpeaking: false });
          }
          return;
        }

        const words: DeepgramWord[] = (alt.words || []).map((w) => ({
          word: w.word,
          start: w.start,
          end: w.end,
          confidence: w.confidence,
          punctuatedWord: w.punctuated_word,
          speaker: w.speaker,
        }));

        const deepgramTranscript: DeepgramTranscript = {
          text: transcript,
          confidence: alt.confidence,
          isFinal: message.is_final ?? false,
          words,
          language: this.options.language || 'en',
          durationMs: (message.duration || 0) * 1000,
          speechFinal: message.speech_final ?? false,
          startTime: message.start || 0,
          endTime: (message.start || 0) + (message.duration || 0),
        };

        this.emit('transcript', deepgramTranscript);

        // Emit VAD speaking event on non-empty interim results
        if (!message.is_final) {
          this.emit('vad', { isSpeaking: true });
        }
        break;
      }

      case 'UtteranceEnd':
        this.emit('vad', { isSpeaking: false });
        this.emit('utteranceEnd', { sessionId: this.sessionId });
        break;

      case 'SpeechStarted':
        this.emit('vad', { isSpeaking: true });
        break;

      case 'Metadata':
        this.logger.debug(`Deepgram metadata for session ${this.sessionId}: ${JSON.stringify(message.metadata)}`);
        break;

      case 'Error':
        this.logger.error(`Deepgram error for session ${this.sessionId}: ${JSON.stringify(message)}`);
        this.emit('error', new Error(`Deepgram error: ${JSON.stringify(message)}`));
        break;

      default:
        this.logger.debug(`Unknown Deepgram message type: ${message.type}`);
    }
  }

  private buildQueryParams(): string {
    const params = new URLSearchParams();

    params.set('model', this.options.model || 'nova-2');
    params.set('language', this.options.language || 'en');
    params.set('encoding', this.options.encoding || 'linear16');
    params.set('sample_rate', String(this.options.sampleRate || 16000));
    params.set('channels', String(this.options.channels || 1));
    params.set('punctuate', String(this.options.punctuate !== false));
    params.set('interim_results', String(this.options.interimResults !== false));
    params.set('smart_format', String(this.options.smartFormat !== false));

    if (this.options.vadEvents !== false) {
      params.set('vad_events', 'true');
    }

    if (this.options.endpointing !== undefined) {
      if (this.options.endpointing === false) {
        params.set('endpointing', 'false');
      } else {
        params.set('endpointing', String(this.options.endpointing));
      }
    }

    if (this.options.utteranceEndMs) {
      params.set('utterance_end_ms', String(this.options.utteranceEndMs));
    }

    if (this.options.diarize) {
      params.set('diarize', 'true');
    }

    if (this.options.detectLanguage) {
      params.set('detect_language', 'true');
    }

    if (this.options.keywords?.length) {
      for (const keyword of this.options.keywords) {
        params.append('keywords', keyword);
      }
    }

    return params.toString();
  }
}
