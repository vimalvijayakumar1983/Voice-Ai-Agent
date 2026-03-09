import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import * as crypto from 'crypto';
import { EventEmitter } from 'events';

/**
 * Twilio Media Stream event types as defined by the Twilio API.
 */
export type TwilioMediaEvent = 'connected' | 'start' | 'media' | 'stop' | 'mark';

export interface TwilioMediaStreamMessage {
  event: TwilioMediaEvent;
  sequenceNumber?: string;
  streamSid?: string;
  start?: {
    streamSid: string;
    accountSid: string;
    callSid: string;
    tracks: string[];
    customParameters: Record<string, string>;
    mediaFormat: {
      encoding: string;
      sampleRate: number;
      channels: number;
    };
  };
  media?: {
    track: string;
    chunk: string;
    timestamp: string;
    payload: string; // base64-encoded audio
  };
  stop?: {
    accountSid: string;
    callSid: string;
  };
  mark?: {
    name: string;
  };
}

export interface MediaStreamSession {
  streamSid: string;
  callSid: string;
  accountSid: string;
  encoding: string;
  sampleRate: number;
  channels: number;
  customParameters: Record<string, string>;
  startedAt: Date;
  sequenceNumber: number;
  isActive: boolean;
}

/**
 * Converts mulaw-encoded audio to 16-bit linear PCM.
 * Twilio Media Streams use mulaw encoding at 8kHz.
 */
function mulawDecode(mulawByte: number): number {
  const MULAW_BIAS = 33;
  let sign: number;
  let exponent: number;
  let mantissa: number;
  let sample: number;

  mulawByte = ~mulawByte & 0xff;
  sign = (mulawByte & 0x80) !== 0 ? -1 : 1;
  exponent = (mulawByte >> 4) & 0x07;
  mantissa = mulawByte & 0x0f;
  sample = mantissa << (exponent + 3);
  sample += MULAW_BIAS << (exponent + 3);
  sample -= MULAW_BIAS;

  return sign * sample;
}

/**
 * Converts 16-bit linear PCM to mulaw encoding.
 */
function mulawEncode(pcmSample: number): number {
  const MULAW_MAX = 0x1fff;
  const MULAW_BIAS = 33;
  const sign = pcmSample < 0 ? 0x80 : 0;

  if (pcmSample < 0) pcmSample = -pcmSample;
  if (pcmSample > MULAW_MAX) pcmSample = MULAW_MAX;

  pcmSample += MULAW_BIAS;

  let exponent = 7;
  const expMask = 0x4000;
  for (; exponent > 0; exponent--) {
    if ((pcmSample & expMask) !== 0) break;
    pcmSample <<= 1;
  }

  const mantissa = (pcmSample >> (exponent + 3)) & 0x0f;
  const mulawByte = ~(sign | (exponent << 4) | mantissa) & 0xff;
  return mulawByte;
}

@Injectable()
export class TwilioMediaStreamProvider extends EventEmitter {
  private readonly logger = new Logger(TwilioMediaStreamProvider.name);
  private readonly accountSid: string;
  private readonly authToken: string;
  private readonly apiBaseUrl: string;
  private readonly activeSessions = new Map<string, MediaStreamSession>();

  constructor(private readonly configService: ConfigService) {
    super();
    this.accountSid = this.configService.get<string>('TWILIO_ACCOUNT_SID', '');
    this.authToken = this.configService.get<string>('TWILIO_AUTH_TOKEN', '');
    this.apiBaseUrl = this.configService.get<string>('API_BASE_URL', 'https://api.example.com');
  }

  /**
   * Generates TwiML that connects an incoming call to a WebSocket media stream.
   * This TwiML is returned in response to Twilio's voice webhook.
   */
  generateMediaStreamTwiml(options: {
    callSid: string;
    agentId: string;
    tenantId: string;
    customParameters?: Record<string, string>;
    initialGreeting?: string;
  }): string {
    const wsUrl = this.apiBaseUrl
      .replace('https://', 'wss://')
      .replace('http://', 'ws://');
    const streamUrl = `${wsUrl}/api/v1/telephony/twilio/media-stream`;

    const paramEntries = {
      callSid: options.callSid,
      agentId: options.agentId,
      tenantId: options.tenantId,
      ...options.customParameters,
    };

    const parameterXml = Object.entries(paramEntries)
      .map(([key, value]) => `        <Parameter name="${this.escapeXml(key)}" value="${this.escapeXml(value)}" />`)
      .join('\n');

    let sayElement = '';
    if (options.initialGreeting) {
      sayElement = `\n  <Say>${this.escapeXml(options.initialGreeting)}</Say>`;
    }

    return `<?xml version="1.0" encoding="UTF-8"?>
<Response>${sayElement}
  <Connect>
    <Stream url="${this.escapeXml(streamUrl)}">
${parameterXml}
    </Stream>
  </Connect>
</Response>`;
  }

  /**
   * Generates TwiML for connecting an outbound call to a media stream.
   */
  generateOutboundTwiml(options: {
    agentId: string;
    tenantId: string;
    recordingDisclosure?: string;
  }): string {
    const wsUrl = this.apiBaseUrl
      .replace('https://', 'wss://')
      .replace('http://', 'ws://');
    const streamUrl = `${wsUrl}/api/v1/telephony/twilio/media-stream`;

    let responseContent = '';
    if (options.recordingDisclosure) {
      responseContent += `\n  <Say>${this.escapeXml(options.recordingDisclosure)}</Say>`;
    }

    responseContent += `
  <Connect>
    <Stream url="${this.escapeXml(streamUrl)}">
      <Parameter name="agentId" value="${this.escapeXml(options.agentId)}" />
      <Parameter name="tenantId" value="${this.escapeXml(options.tenantId)}" />
    </Stream>
  </Connect>`;

    return `<?xml version="1.0" encoding="UTF-8"?>
<Response>${responseContent}
</Response>`;
  }

  /**
   * Handles an incoming WebSocket message from Twilio Media Streams.
   * Parses the message, manages session state, and emits typed events.
   */
  handleWebSocketMessage(
    rawMessage: string | Buffer,
    wsConnectionId: string,
  ): { event: TwilioMediaEvent; session?: MediaStreamSession; pcmAudio?: Buffer } | null {
    let message: TwilioMediaStreamMessage;
    try {
      const messageStr = typeof rawMessage === 'string' ? rawMessage : rawMessage.toString('utf-8');
      message = JSON.parse(messageStr);
    } catch (err) {
      this.logger.error(`Failed to parse Twilio media stream message: ${(err as Error).message}`);
      return null;
    }

    switch (message.event) {
      case 'connected':
        this.logger.log(`Media stream connected for WebSocket ${wsConnectionId}`);
        this.emit('stream:connected', { wsConnectionId });
        return { event: 'connected' };

      case 'start': {
        if (!message.start) {
          this.logger.warn('Received start event without start payload');
          return null;
        }

        const session: MediaStreamSession = {
          streamSid: message.start.streamSid,
          callSid: message.start.callSid,
          accountSid: message.start.accountSid,
          encoding: message.start.mediaFormat.encoding,
          sampleRate: message.start.mediaFormat.sampleRate,
          channels: message.start.mediaFormat.channels,
          customParameters: message.start.customParameters,
          startedAt: new Date(),
          sequenceNumber: 0,
          isActive: true,
        };

        this.activeSessions.set(message.start.streamSid, session);
        this.logger.log(
          `Media stream started: streamSid=${session.streamSid}, callSid=${session.callSid}, ` +
          `encoding=${session.encoding}, sampleRate=${session.sampleRate}`,
        );

        this.emit('stream:start', { session, wsConnectionId });
        return { event: 'start', session };
      }

      case 'media': {
        if (!message.media || !message.streamSid) {
          return null;
        }

        const session = this.activeSessions.get(message.streamSid);
        if (!session || !session.isActive) {
          return null;
        }

        session.sequenceNumber = parseInt(message.sequenceNumber || '0', 10);

        // Decode base64 mulaw audio to PCM
        const mulawBuffer = Buffer.from(message.media.payload, 'base64');
        const pcmAudio = this.mulawToPcm(mulawBuffer);

        this.emit('stream:audio', {
          streamSid: message.streamSid,
          callSid: session.callSid,
          pcmAudio,
          rawPayload: mulawBuffer,
          timestamp: message.media.timestamp,
          track: message.media.track,
          wsConnectionId,
        });

        return { event: 'media', session, pcmAudio };
      }

      case 'stop': {
        const streamSid = message.streamSid;
        if (streamSid) {
          const session = this.activeSessions.get(streamSid);
          if (session) {
            session.isActive = false;
            this.logger.log(
              `Media stream stopped: streamSid=${streamSid}, callSid=${session.callSid}`,
            );
            this.emit('stream:stop', { session, wsConnectionId });
            this.activeSessions.delete(streamSid);
            return { event: 'stop', session };
          }
        }

        this.logger.log(`Media stream stop event for unknown stream ${streamSid}`);
        return { event: 'stop' };
      }

      case 'mark': {
        if (message.mark && message.streamSid) {
          this.emit('stream:mark', {
            streamSid: message.streamSid,
            markName: message.mark.name,
            wsConnectionId,
          });
        }
        return { event: 'mark' };
      }

      default:
        this.logger.debug(`Unknown media stream event: ${message.event}`);
        return null;
    }
  }

  /**
   * Sends audio back to Twilio over the WebSocket connection.
   * Converts PCM audio to mulaw and sends as a media message.
   */
  createMediaMessage(streamSid: string, pcmAudio: Buffer): string {
    const mulawAudio = this.pcmToMulaw(pcmAudio);
    const payload = mulawAudio.toString('base64');

    return JSON.stringify({
      event: 'media',
      streamSid,
      media: {
        payload,
      },
    });
  }

  /**
   * Sends a mark event to track when audio has been played.
   */
  createMarkMessage(streamSid: string, markName: string): string {
    return JSON.stringify({
      event: 'mark',
      streamSid,
      mark: {
        name: markName,
      },
    });
  }

  /**
   * Sends a clear event to stop any pending audio from playing.
   * Useful for barge-in (interruption) support.
   */
  createClearMessage(streamSid: string): string {
    return JSON.stringify({
      event: 'clear',
      streamSid,
    });
  }

  /**
   * Validates a Twilio webhook request signature to ensure authenticity.
   */
  validateSignature(
    url: string,
    params: Record<string, string>,
    signature: string,
  ): boolean {
    if (!this.authToken) {
      this.logger.warn('No auth token configured, skipping signature validation');
      return true;
    }

    // Build the data string: url + sorted params
    let data = url;
    const sortedKeys = Object.keys(params).sort();
    for (const key of sortedKeys) {
      data += key + params[key];
    }

    const expectedSignature = crypto
      .createHmac('sha1', this.authToken)
      .update(data)
      .digest('base64');

    return crypto.timingSafeEqual(
      Buffer.from(signature),
      Buffer.from(expectedSignature),
    );
  }

  /**
   * Validates a Twilio body hash signature (for POST with JSON body).
   */
  validateBodySignature(
    url: string,
    body: string,
    signature: string,
  ): boolean {
    if (!this.authToken) {
      this.logger.warn('No auth token configured, skipping body signature validation');
      return true;
    }

    const bodyHash = crypto.createHash('sha256').update(body).digest('hex');
    const urlWithHash = `${url}?bodySHA256=${bodyHash}`;

    const expectedSignature = crypto
      .createHmac('sha1', this.authToken)
      .update(urlWithHash)
      .digest('base64');

    try {
      return crypto.timingSafeEqual(
        Buffer.from(signature),
        Buffer.from(expectedSignature),
      );
    } catch {
      return false;
    }
  }

  /**
   * Returns the session for a given streamSid.
   */
  getSession(streamSid: string): MediaStreamSession | undefined {
    return this.activeSessions.get(streamSid);
  }

  /**
   * Returns the session for a given callSid.
   */
  getSessionByCallSid(callSid: string): MediaStreamSession | undefined {
    for (const session of this.activeSessions.values()) {
      if (session.callSid === callSid) return session;
    }
    return undefined;
  }

  /**
   * Returns the count of active media stream sessions.
   */
  getActiveSessionCount(): number {
    return this.activeSessions.size;
  }

  /**
   * Forcibly ends a media stream session.
   */
  endSession(streamSid: string): void {
    const session = this.activeSessions.get(streamSid);
    if (session) {
      session.isActive = false;
      this.activeSessions.delete(streamSid);
      this.logger.log(`Forcibly ended media stream session: ${streamSid}`);
    }
  }

  /**
   * Converts mulaw-encoded buffer to 16-bit signed PCM buffer.
   */
  mulawToPcm(mulawBuffer: Buffer): Buffer {
    const pcmBuffer = Buffer.alloc(mulawBuffer.length * 2);
    for (let i = 0; i < mulawBuffer.length; i++) {
      const pcmSample = mulawDecode(mulawBuffer[i]);
      pcmBuffer.writeInt16LE(pcmSample, i * 2);
    }
    return pcmBuffer;
  }

  /**
   * Converts 16-bit signed PCM buffer to mulaw-encoded buffer.
   */
  pcmToMulaw(pcmBuffer: Buffer): Buffer {
    const mulawBuffer = Buffer.alloc(pcmBuffer.length / 2);
    for (let i = 0; i < mulawBuffer.length; i++) {
      const pcmSample = pcmBuffer.readInt16LE(i * 2);
      mulawBuffer[i] = mulawEncode(pcmSample);
    }
    return mulawBuffer;
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
