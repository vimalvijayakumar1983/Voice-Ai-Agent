import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { EventEmitter } from 'events';

export interface AzureTTSOptions {
  voice?: string;
  language?: string;
  outputFormat?: AzureOutputFormat;
  rate?: string;     // e.g., "+20%", "-10%", "slow", "fast"
  pitch?: string;    // e.g., "+10Hz", "-5%", "high", "low"
  volume?: string;   // e.g., "+10%", "loud", "soft"
  style?: string;    // e.g., "cheerful", "sad", "angry", "excited"
  styleDegree?: number; // 0.01 to 2.0
  role?: string;     // e.g., "Girl", "Boy", "OlderAdultFemale"
  ssml?: string;     // Raw SSML to use instead of generating it
}

export type AzureOutputFormat =
  | 'raw-16khz-16bit-mono-pcm'
  | 'raw-8khz-8bit-mono-mulaw'
  | 'raw-24khz-16bit-mono-pcm'
  | 'audio-16khz-128kbitrate-mono-mp3'
  | 'audio-24khz-160kbitrate-mono-mp3'
  | 'riff-16khz-16bit-mono-pcm'
  | 'riff-24khz-16bit-mono-pcm';

export interface AzureVoice {
  name: string;
  displayName: string;
  locale: string;
  gender: string;
  styleList?: string[];
  rolePlayList?: string[];
}

@Injectable()
export class AzureTTSProvider extends EventEmitter {
  private readonly logger = new Logger(AzureTTSProvider.name);
  private readonly subscriptionKey: string;
  private readonly region: string;
  private readonly defaultVoice: string;
  private readonly defaultLanguage: string;
  private accessToken: string | null = null;
  private tokenExpiresAt = 0;

  constructor(private readonly configService: ConfigService) {
    super();
    this.subscriptionKey = this.configService.get<string>('AZURE_SPEECH_KEY', '');
    this.region = this.configService.get<string>('AZURE_SPEECH_REGION', 'eastus');
    this.defaultVoice = this.configService.get<string>(
      'AZURE_TTS_DEFAULT_VOICE',
      'en-US-JennyNeural',
    );
    this.defaultLanguage = this.configService.get<string>('AZURE_TTS_DEFAULT_LANGUAGE', 'en-US');
  }

  /**
   * Synthesizes text to speech using Azure Cognitive Services TTS REST API.
   * Returns the complete audio buffer.
   */
  async synthesize(text: string, options: AzureTTSOptions = {}): Promise<Buffer> {
    const voice = options.voice || this.defaultVoice;
    const outputFormat = options.outputFormat || 'raw-16khz-16bit-mono-pcm';

    this.logger.log(`Azure TTS synthesizing ${text.length} chars with voice ${voice}`);

    const ssml = options.ssml || this.buildSSML(text, options);
    const token = await this.getAccessToken();

    const ttsUrl = `https://${this.region}.tts.speech.microsoft.com/cognitiveservices/v1`;

    try {
      const response = await fetch(ttsUrl, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/ssml+xml',
          'X-Microsoft-OutputFormat': outputFormat,
          'User-Agent': 'VoiceAIAgent/1.0',
        },
        body: ssml,
      });

      if (!response.ok) {
        const errorBody = await response.text();
        throw new Error(`Azure TTS error ${response.status}: ${errorBody}`);
      }

      const arrayBuffer = await response.arrayBuffer();
      const audioBuffer = Buffer.from(arrayBuffer);

      this.logger.log(`Azure TTS synthesized ${audioBuffer.length} bytes`);
      return audioBuffer;
    } catch (error) {
      this.logger.error(`Azure TTS synthesis failed: ${(error as Error).message}`);
      throw error;
    }
  }

  /**
   * Synthesizes text with streaming, sending audio chunks as they become available.
   * Uses chunked transfer to provide lower latency than full synthesis.
   */
  async synthesizeStream(
    text: string,
    options: AzureTTSOptions = {},
    onAudioChunk?: (chunk: Buffer) => void,
  ): Promise<Buffer> {
    const voice = options.voice || this.defaultVoice;
    const outputFormat = options.outputFormat || 'raw-16khz-16bit-mono-pcm';

    this.logger.log(`Azure TTS streaming ${text.length} chars with voice ${voice}`);

    const ssml = options.ssml || this.buildSSML(text, options);
    const token = await this.getAccessToken();

    const ttsUrl = `https://${this.region}.tts.speech.microsoft.com/cognitiveservices/v1`;

    try {
      const response = await fetch(ttsUrl, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/ssml+xml',
          'X-Microsoft-OutputFormat': outputFormat,
          'User-Agent': 'VoiceAIAgent/1.0',
        },
        body: ssml,
      });

      if (!response.ok) {
        const errorBody = await response.text();
        throw new Error(`Azure TTS stream error ${response.status}: ${errorBody}`);
      }

      if (!response.body) {
        throw new Error('Azure TTS response has no body');
      }

      const audioChunks: Buffer[] = [];
      const reader = response.body.getReader();

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = Buffer.from(value);
        audioChunks.push(chunk);

        if (onAudioChunk) {
          onAudioChunk(chunk);
        }

        this.emit('audio', { chunk, isFinal: false });
      }

      const fullAudio = Buffer.concat(audioChunks);
      this.emit('audio', { chunk: Buffer.alloc(0), isFinal: true });

      this.logger.log(`Azure TTS streamed ${fullAudio.length} bytes total`);
      return fullAudio;
    } catch (error) {
      this.logger.error(`Azure TTS stream failed: ${(error as Error).message}`);
      throw error;
    }
  }

  /**
   * Retrieves available neural voices from Azure.
   */
  async getVoices(locale?: string): Promise<AzureVoice[]> {
    const token = await this.getAccessToken();
    const url = `https://${this.region}.tts.speech.microsoft.com/cognitiveservices/voices/list`;

    try {
      const response = await fetch(url, {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });

      if (!response.ok) {
        throw new Error(`Azure voices list error ${response.status}`);
      }

      const voices = (await response.json()) as Array<{
        Name: string;
        DisplayName: string;
        LocalName: string;
        Locale: string;
        Gender: string;
        VoiceType: string;
        StyleList?: string[];
        RolePlayList?: string[];
      }>;

      let filtered = voices.filter((v) => v.VoiceType === 'Neural');
      if (locale) {
        filtered = filtered.filter((v) => v.Locale.startsWith(locale));
      }

      return filtered.map((v) => ({
        name: v.Name,
        displayName: v.DisplayName,
        locale: v.Locale,
        gender: v.Gender,
        styleList: v.StyleList,
        rolePlayList: v.RolePlayList,
      }));
    } catch (error) {
      this.logger.error(`Failed to fetch Azure voices: ${(error as Error).message}`);
      return [];
    }
  }

  /**
   * Builds SSML (Speech Synthesis Markup Language) with full prosody control.
   */
  buildSSML(text: string, options: AzureTTSOptions = {}): string {
    const voice = options.voice || this.defaultVoice;
    const language = options.language || this.defaultLanguage;

    const escapedText = this.escapeXml(text);

    // Build prosody attributes
    const prosodyAttrs: string[] = [];
    if (options.rate) prosodyAttrs.push(`rate="${options.rate}"`);
    if (options.pitch) prosodyAttrs.push(`pitch="${options.pitch}"`);
    if (options.volume) prosodyAttrs.push(`volume="${options.volume}"`);

    let innerContent = escapedText;

    // Wrap with prosody if any attributes are set
    if (prosodyAttrs.length > 0) {
      innerContent = `<prosody ${prosodyAttrs.join(' ')}>${innerContent}</prosody>`;
    }

    // Wrap with style if specified
    if (options.style) {
      const styleDegree = options.styleDegree !== undefined ? ` styledegree="${options.styleDegree}"` : '';
      const role = options.role ? ` role="${options.role}"` : '';
      innerContent =
        `<mstts:express-as style="${options.style}"${styleDegree}${role}>` +
        `${innerContent}</mstts:express-as>`;
    }

    return `<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" ` +
      `xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="${language}">` +
      `<voice name="${voice}">${innerContent}</voice></speak>`;
  }

  /**
   * Gets or refreshes the Azure access token using the subscription key.
   * Tokens are valid for 10 minutes; we refresh at 9 minutes.
   */
  private async getAccessToken(): Promise<string> {
    const now = Date.now();
    if (this.accessToken && this.tokenExpiresAt > now) {
      return this.accessToken;
    }

    if (!this.subscriptionKey) {
      throw new Error('Azure Speech subscription key not configured');
    }

    const tokenUrl = `https://${this.region}.api.cognitive.microsoft.com/sts/v1.0/issueToken`;

    try {
      const response = await fetch(tokenUrl, {
        method: 'POST',
        headers: {
          'Ocp-Apim-Subscription-Key': this.subscriptionKey,
          'Content-Length': '0',
        },
      });

      if (!response.ok) {
        throw new Error(`Azure token error ${response.status}: ${await response.text()}`);
      }

      this.accessToken = await response.text();
      this.tokenExpiresAt = now + 9 * 60 * 1000; // Refresh 1 minute before expiry

      this.logger.debug('Azure Speech access token refreshed');
      return this.accessToken;
    } catch (error) {
      this.logger.error(`Failed to get Azure access token: ${(error as Error).message}`);
      throw error;
    }
  }

  private escapeXml(str: string): string {
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&apos;');
  }
}
