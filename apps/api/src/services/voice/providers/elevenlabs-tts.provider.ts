import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { EventEmitter } from 'events';
import WebSocket from 'ws';

export interface ElevenLabsTTSOptions {
  voiceId?: string;
  modelId?: string;
  stability?: number;
  similarityBoost?: number;
  style?: number;
  useSpeakerBoost?: boolean;
  outputFormat?: 'pcm_16000' | 'pcm_22050' | 'pcm_24000' | 'pcm_44100' | 'mp3_44100' | 'ulaw_8000';
  optimizeStreamingLatency?: number;
  language?: string;
}

export interface ElevenLabsVoice {
  voiceId: string;
  name: string;
  category: string;
  labels: Record<string, string>;
}

interface ElevenLabsWSMessage {
  audio?: string; // base64-encoded audio
  isFinal?: boolean;
  normalizedAlignment?: {
    char_start_times_ms: number[];
    chars_durations_ms: number[];
    chars: string[];
  };
  alignment?: {
    char_start_times_ms: number[];
    chars_durations_ms: number[];
    chars: string[];
  };
  error?: string;
  message?: string;
}

@Injectable()
export class ElevenLabsTTSProvider extends EventEmitter {
  private readonly logger = new Logger(ElevenLabsTTSProvider.name);
  private readonly apiKey: string;
  private readonly baseUrl: string;
  private readonly wsBaseUrl: string;
  private readonly defaultVoiceId: string;
  private readonly defaultModelId: string;
  private activeSessions = new Map<string, ElevenLabsTTSSession>();

  constructor(private readonly configService: ConfigService) {
    super();
    this.apiKey = this.configService.get<string>('ELEVENLABS_API_KEY', '');
    this.baseUrl = this.configService.get<string>('ELEVENLABS_API_URL', 'https://api.elevenlabs.io/v1');
    this.wsBaseUrl = this.configService.get<string>('ELEVENLABS_WS_URL', 'wss://api.elevenlabs.io/v1');
    this.defaultVoiceId = this.configService.get<string>('ELEVENLABS_DEFAULT_VOICE_ID', '21m00Tcm4TlvDq8ikWAM');
    this.defaultModelId = this.configService.get<string>('ELEVENLABS_MODEL_ID', 'eleven_turbo_v2_5');
  }

  /**
   * Creates a WebSocket streaming TTS session for low-latency audio synthesis.
   * Text can be sent incrementally, and audio chunks arrive as they're generated.
   */
  createStream(
    sessionId: string,
    options: ElevenLabsTTSOptions = {},
  ): ElevenLabsTTSSession {
    const existingSession = this.activeSessions.get(sessionId);
    if (existingSession && existingSession.isActive) {
      existingSession.close();
    }

    const voiceId = options.voiceId || this.defaultVoiceId;
    const modelId = options.modelId || this.defaultModelId;
    const outputFormat = options.outputFormat || 'pcm_16000';

    const session = new ElevenLabsTTSSession(
      sessionId,
      this.apiKey,
      this.wsBaseUrl,
      voiceId,
      modelId,
      outputFormat,
      options,
      this.logger,
    );

    session.on('audio', (data: { audio: Buffer; isFinal: boolean }) => {
      this.emit('audio', { sessionId, ...data });
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
   * Synthesizes text to speech using the REST API (non-streaming).
   * Returns the complete audio buffer.
   */
  async synthesize(
    text: string,
    options: ElevenLabsTTSOptions = {},
  ): Promise<Buffer> {
    const voiceId = options.voiceId || this.defaultVoiceId;
    const modelId = options.modelId || this.defaultModelId;
    const outputFormat = options.outputFormat || 'pcm_16000';

    const url = `${this.baseUrl}/text-to-speech/${voiceId}?output_format=${outputFormat}`;

    this.logger.log(`ElevenLabs TTS synthesizing ${text.length} chars with voice ${voiceId}`);

    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'xi-api-key': this.apiKey,
        },
        body: JSON.stringify({
          text,
          model_id: modelId,
          voice_settings: {
            stability: options.stability ?? 0.5,
            similarity_boost: options.similarityBoost ?? 0.75,
            style: options.style ?? 0,
            use_speaker_boost: options.useSpeakerBoost ?? true,
          },
        }),
      });

      if (!response.ok) {
        const errorBody = await response.text();
        throw new Error(`ElevenLabs API error ${response.status}: ${errorBody}`);
      }

      const arrayBuffer = await response.arrayBuffer();
      return Buffer.from(arrayBuffer);
    } catch (error) {
      this.logger.error(`ElevenLabs TTS synthesis failed: ${(error as Error).message}`);
      throw error;
    }
  }

  /**
   * Synthesizes text with sentence-level chunking for optimal streaming latency.
   * Splits text into sentences and sends each sentence, collecting audio progressively.
   */
  async synthesizeWithChunking(
    text: string,
    options: ElevenLabsTTSOptions = {},
    onAudioChunk?: (chunk: Buffer) => void,
  ): Promise<Buffer> {
    const sentences = this.splitIntoSentences(text);
    const audioBuffers: Buffer[] = [];

    const session = this.createStream(`chunked-${Date.now()}`, options);

    return new Promise<Buffer>((resolve, reject) => {
      session.on('audio', (data: { audio: Buffer; isFinal: boolean }) => {
        audioBuffers.push(data.audio);
        if (onAudioChunk) {
          onAudioChunk(data.audio);
        }
        if (data.isFinal) {
          session.close();
          resolve(Buffer.concat(audioBuffers));
        }
      });

      session.on('error', (error: Error) => {
        session.close();
        reject(error);
      });

      // Wait for connection then send sentences
      session.on('ready', () => {
        for (const sentence of sentences) {
          session.sendText(sentence);
        }
        session.flush();
      });
    });
  }

  /**
   * Sends text to an existing streaming session.
   */
  sendText(sessionId: string, text: string): void {
    const session = this.activeSessions.get(sessionId);
    if (!session || !session.isActive) {
      this.logger.warn(`No active TTS session ${sessionId}`);
      return;
    }
    session.sendText(text);
  }

  /**
   * Flushes and closes a streaming session, triggering final audio output.
   */
  flushStream(sessionId: string): void {
    const session = this.activeSessions.get(sessionId);
    if (session) {
      session.flush();
    }
  }

  /**
   * Closes a streaming session.
   */
  closeStream(sessionId: string): void {
    const session = this.activeSessions.get(sessionId);
    if (session) {
      session.close();
      this.activeSessions.delete(sessionId);
    }
  }

  /**
   * Gets available voices from ElevenLabs API.
   */
  async getVoices(): Promise<ElevenLabsVoice[]> {
    try {
      const response = await fetch(`${this.baseUrl}/voices`, {
        headers: { 'xi-api-key': this.apiKey },
      });

      if (!response.ok) {
        throw new Error(`ElevenLabs voices API error ${response.status}`);
      }

      const data = (await response.json()) as {
        voices: Array<{
          voice_id: string;
          name: string;
          category: string;
          labels: Record<string, string>;
        }>;
      };

      return data.voices.map((v) => ({
        voiceId: v.voice_id,
        name: v.name,
        category: v.category,
        labels: v.labels || {},
      }));
    } catch (error) {
      this.logger.error(`Failed to fetch ElevenLabs voices: ${(error as Error).message}`);
      return [];
    }
  }

  getActiveSessionCount(): number {
    return this.activeSessions.size;
  }

  /**
   * Splits text into sentences for chunked streaming.
   */
  private splitIntoSentences(text: string): string[] {
    const sentenceRegex = /[^.!?]*[.!?]+\s*/g;
    const matches = text.match(sentenceRegex);
    if (!matches) return [text];

    // If there's remaining text after the last sentence-ending punctuation, include it
    const matched = matches.join('');
    const remainder = text.slice(matched.length).trim();
    if (remainder) {
      matches.push(remainder);
    }

    return matches.filter((s) => s.trim().length > 0);
  }
}

export class ElevenLabsTTSSession extends EventEmitter {
  private ws: WebSocket | null = null;
  private _isActive = false;
  private textBuffer: string[] = [];

  get isActive(): boolean {
    return this._isActive;
  }

  constructor(
    public readonly sessionId: string,
    private readonly apiKey: string,
    private readonly wsBaseUrl: string,
    private readonly voiceId: string,
    private readonly modelId: string,
    private readonly outputFormat: string,
    private readonly options: ElevenLabsTTSOptions,
    private readonly logger: Logger,
  ) {
    super();
    this.connect();
  }

  /**
   * Sends text to ElevenLabs for synthesis.
   * Text is sent incrementally - ElevenLabs handles sentence boundary detection.
   */
  sendText(text: string): void {
    if (!this._isActive || !this.ws || this.ws.readyState !== WebSocket.OPEN) {
      this.textBuffer.push(text);
      return;
    }

    try {
      this.ws.send(JSON.stringify({
        text,
        try_trigger_generation: true,
      }));
    } catch (error) {
      this.logger.error(`Failed to send text for session ${this.sessionId}: ${(error as Error).message}`);
    }
  }

  /**
   * Sends an empty string to flush any remaining text and signal end of input.
   */
  flush(): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      try {
        this.ws.send(JSON.stringify({
          text: '',
        }));
      } catch (error) {
        this.logger.error(`Failed to flush TTS session ${this.sessionId}: ${(error as Error).message}`);
      }
    }
  }

  /**
   * Closes the WebSocket connection.
   */
  close(): void {
    this._isActive = false;
    this.textBuffer = [];

    if (this.ws) {
      try {
        if (this.ws.readyState === WebSocket.OPEN) {
          // Send EOS (end of sequence) signal
          this.ws.send(JSON.stringify({ text: '' }));
        }
        this.ws.close();
      } catch (error) {
        this.logger.debug(`Error closing ElevenLabs WebSocket: ${(error as Error).message}`);
      }
      this.ws = null;
    }

    this.emit('close');
  }

  private connect(): void {
    const latencyOpt = this.options.optimizeStreamingLatency ?? 3;
    const url =
      `${this.wsBaseUrl}/text-to-speech/${this.voiceId}/stream-input` +
      `?model_id=${encodeURIComponent(this.modelId)}` +
      `&output_format=${this.outputFormat}` +
      `&optimize_streaming_latency=${latencyOpt}`;

    this.logger.log(
      `Connecting ElevenLabs TTS for session ${this.sessionId}, voice=${this.voiceId}, format=${this.outputFormat}`,
    );

    try {
      this.ws = new WebSocket(url, {
        headers: {
          'xi-api-key': this.apiKey,
        },
      });
    } catch (error) {
      this.logger.error(`Failed to create ElevenLabs WebSocket: ${(error as Error).message}`);
      this.emit('error', error);
      return;
    }

    this.ws.on('open', () => {
      this._isActive = true;
      this.logger.log(`ElevenLabs TTS connected for session ${this.sessionId}`);

      // Send initial configuration message (BOS - beginning of stream)
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({
          text: ' ',
          voice_settings: {
            stability: this.options.stability ?? 0.5,
            similarity_boost: this.options.similarityBoost ?? 0.75,
            style: this.options.style ?? 0,
            use_speaker_boost: this.options.useSpeakerBoost ?? true,
          },
          generation_config: {
            chunk_length_schedule: [50, 90, 120, 150],
          },
        }));
      }

      // Flush any buffered text
      while (this.textBuffer.length > 0) {
        const text = this.textBuffer.shift();
        if (text) this.sendText(text);
      }

      this.emit('ready');
    });

    this.ws.on('message', (data: WebSocket.Data) => {
      try {
        const message: ElevenLabsWSMessage = JSON.parse(data.toString());
        this.handleMessage(message);
      } catch (error) {
        this.logger.error(`Failed to parse ElevenLabs message: ${(error as Error).message}`);
      }
    });

    this.ws.on('error', (error: Error) => {
      this.logger.error(`ElevenLabs WebSocket error for session ${this.sessionId}: ${error.message}`);
      this.emit('error', error);
    });

    this.ws.on('close', (code: number) => {
      this.logger.log(`ElevenLabs WebSocket closed for session ${this.sessionId}: code=${code}`);
      this._isActive = false;
      this.emit('close');
    });
  }

  private handleMessage(message: ElevenLabsWSMessage): void {
    if (message.error) {
      this.logger.error(`ElevenLabs error: ${message.error} - ${message.message}`);
      this.emit('error', new Error(`ElevenLabs: ${message.error} - ${message.message}`));
      return;
    }

    if (message.audio) {
      const audioBuffer = Buffer.from(message.audio, 'base64');
      this.emit('audio', {
        audio: audioBuffer,
        isFinal: message.isFinal ?? false,
      });
    } else if (message.isFinal) {
      // Final message with no audio - stream complete
      this.emit('audio', {
        audio: Buffer.alloc(0),
        isFinal: true,
      });
    }
  }
}
