import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import * as crypto from 'crypto';
import {
  ITelephonyProvider,
  CallOptions,
  CallResult,
  CallStatus,
} from '../telephony.interface';

/**
 * NCCO (Nexmo Call Control Object) action types for Vonage Voice API.
 */
export interface NccoAction {
  action: 'talk' | 'stream' | 'input' | 'connect' | 'record' | 'conversation' | 'notify';
  [key: string]: unknown;
}

export interface VonageCallEvent {
  uuid: string;
  conversation_uuid: string;
  status: string;
  direction: string;
  timestamp: string;
  from?: string;
  to?: string;
  duration?: string;
  rate?: string;
  price?: string;
}

export interface VonageWebSocketEndpoint {
  type: 'websocket';
  uri: string;
  'content-type': 'audio/l16;rate=16000' | 'audio/l16;rate=8000';
  headers?: Record<string, string>;
}

@Injectable()
export class VonageVoiceProvider implements ITelephonyProvider {
  readonly providerName = 'vonage';
  private readonly logger = new Logger(VonageVoiceProvider.name);
  private readonly apiKey: string;
  private readonly apiSecret: string;
  private readonly applicationId: string;
  private readonly privateKey: string;
  private readonly apiBaseUrl: string;
  private readonly vonageApiUrl = 'https://api.nexmo.com/v1/calls';

  constructor(private readonly configService: ConfigService) {
    this.apiKey = this.configService.get<string>('VONAGE_API_KEY', '');
    this.apiSecret = this.configService.get<string>('VONAGE_API_SECRET', '');
    this.applicationId = this.configService.get<string>('VONAGE_APPLICATION_ID', '');
    this.privateKey = this.configService.get<string>('VONAGE_PRIVATE_KEY', '');
    this.apiBaseUrl = this.configService.get<string>('API_BASE_URL', 'https://api.example.com');
  }

  async makeCall(options: CallOptions): Promise<CallResult> {
    this.logger.log(`[Vonage] Making call to ${options.to} from ${options.from}`);

    const jwt = this.generateJwt();

    const ncco = this.buildCallNcco({
      agentId: (options.metadata?.agentId as string) || '',
      tenantId: (options.metadata?.tenantId as string) || '',
      recordingDisclosure: options.metadata?.recordingDisclosure as string,
    });

    const requestBody = {
      to: [{ type: 'phone', number: options.to.replace(/\+/g, '') }],
      from: { type: 'phone', number: options.from.replace(/\+/g, '') },
      ncco,
      event_url: [
        `${this.apiBaseUrl}/api/v1/telephony/vonage/event`,
      ],
      event_method: 'POST',
    };

    try {
      const response = await fetch(this.vonageApiUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${jwt}`,
        },
        body: JSON.stringify(requestBody),
      });

      if (!response.ok) {
        const errorBody = await response.text();
        throw new Error(`Vonage API error ${response.status}: ${errorBody}`);
      }

      const data = (await response.json()) as { uuid: string; status: string; conversation_uuid: string };
      this.logger.log(`[Vonage] Call initiated: uuid=${data.uuid}`);

      return {
        callSid: data.uuid,
        status: data.status || 'started',
        provider: this.providerName,
      };
    } catch (error) {
      this.logger.error(`[Vonage] Failed to make call: ${(error as Error).message}`);
      throw error;
    }
  }

  async answerCall(callSid: string, actions: Record<string, unknown>): Promise<void> {
    this.logger.log(`[Vonage] Answering call ${callSid}`);

    const jwt = this.generateJwt();
    const ncco = actions.ncco as NccoAction[] || this.buildCallNcco({
      agentId: (actions.agentId as string) || '',
      tenantId: (actions.tenantId as string) || '',
    });

    try {
      const response = await fetch(`${this.vonageApiUrl}/${callSid}`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${jwt}`,
        },
        body: JSON.stringify({
          action: 'transfer',
          destination: { type: 'ncco', ncco },
        }),
      });

      if (!response.ok) {
        const errorBody = await response.text();
        throw new Error(`Vonage transfer error ${response.status}: ${errorBody}`);
      }
    } catch (error) {
      this.logger.error(`[Vonage] Failed to answer call: ${(error as Error).message}`);
      throw error;
    }
  }

  async endCall(callSid: string): Promise<void> {
    this.logger.log(`[Vonage] Ending call ${callSid}`);

    const jwt = this.generateJwt();

    try {
      const response = await fetch(`${this.vonageApiUrl}/${callSid}`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${jwt}`,
        },
        body: JSON.stringify({ action: 'hangup' }),
      });

      if (!response.ok) {
        const errorBody = await response.text();
        throw new Error(`Vonage hangup error ${response.status}: ${errorBody}`);
      }

      this.logger.log(`[Vonage] Call ${callSid} ended`);
    } catch (error) {
      this.logger.error(`[Vonage] Failed to end call: ${(error as Error).message}`);
      throw error;
    }
  }

  async transferCall(callSid: string, targetNumber: string): Promise<void> {
    this.logger.log(`[Vonage] Transferring call ${callSid} to ${targetNumber}`);

    const jwt = this.generateJwt();
    const ncco: NccoAction[] = [
      {
        action: 'talk',
        text: 'Please hold while we transfer your call.',
        language: 'en-US',
      },
      {
        action: 'connect',
        from: this.configService.get<string>('VONAGE_FROM_NUMBER', ''),
        endpoint: [
          { type: 'phone', number: targetNumber.replace(/\+/g, '') },
        ],
      },
    ];

    try {
      const response = await fetch(`${this.vonageApiUrl}/${callSid}`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${jwt}`,
        },
        body: JSON.stringify({
          action: 'transfer',
          destination: { type: 'ncco', ncco },
        }),
      });

      if (!response.ok) {
        const errorBody = await response.text();
        throw new Error(`Vonage transfer error ${response.status}: ${errorBody}`);
      }

      this.logger.log(`[Vonage] Call ${callSid} transferred to ${targetNumber}`);
    } catch (error) {
      this.logger.error(`[Vonage] Failed to transfer call: ${(error as Error).message}`);
      throw error;
    }
  }

  async getCallStatus(callSid: string): Promise<CallStatus> {
    this.logger.log(`[Vonage] Getting status for call ${callSid}`);

    const jwt = this.generateJwt();

    try {
      const response = await fetch(`${this.vonageApiUrl}/${callSid}`, {
        method: 'GET',
        headers: {
          Authorization: `Bearer ${jwt}`,
        },
      });

      if (!response.ok) {
        throw new Error(`Vonage status error ${response.status}`);
      }

      const data = (await response.json()) as {
        uuid: string;
        status: string;
        direction: string;
        duration?: string;
        start_time?: string;
        end_time?: string;
      };

      return {
        callSid: data.uuid,
        status: this.mapVonageStatus(data.status),
        direction: data.direction || 'outbound',
        duration: data.duration ? parseInt(data.duration, 10) : undefined,
        startTime: data.start_time ? new Date(data.start_time) : undefined,
        endTime: data.end_time ? new Date(data.end_time) : undefined,
      };
    } catch (error) {
      this.logger.error(`[Vonage] Failed to get call status: ${(error as Error).message}`);
      throw error;
    }
  }

  async sendDTMF(callSid: string, digits: string): Promise<void> {
    this.logger.log(`[Vonage] Sending DTMF ${digits} to call ${callSid}`);

    const jwt = this.generateJwt();

    try {
      const response = await fetch(`${this.vonageApiUrl}/${callSid}/dtmf`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${jwt}`,
        },
        body: JSON.stringify({ digits }),
      });

      if (!response.ok) {
        throw new Error(`Vonage DTMF error ${response.status}`);
      }
    } catch (error) {
      this.logger.error(`[Vonage] Failed to send DTMF: ${(error as Error).message}`);
      throw error;
    }
  }

  /**
   * Generates an NCCO answer response for incoming calls that connects to a WebSocket.
   */
  generateAnswerNcco(options: {
    agentId: string;
    tenantId: string;
    callUuid: string;
    recordingDisclosure?: string;
  }): NccoAction[] {
    return this.buildCallNcco(options);
  }

  /**
   * Parses a Vonage event webhook payload.
   */
  parseEventWebhook(body: Record<string, unknown>): VonageCallEvent {
    return {
      uuid: body.uuid as string,
      conversation_uuid: body.conversation_uuid as string,
      status: body.status as string,
      direction: body.direction as string,
      timestamp: body.timestamp as string,
      from: body.from as string | undefined,
      to: body.to as string | undefined,
      duration: body.duration as string | undefined,
      rate: body.rate as string | undefined,
      price: body.price as string | undefined,
    };
  }

  /**
   * Validates the Vonage webhook signature using JWT or signing secret.
   */
  validateWebhookSignature(
    token: string,
    body: string,
  ): boolean {
    if (!this.apiSecret) {
      this.logger.warn('No API secret configured, skipping Vonage signature validation');
      return true;
    }

    try {
      const expectedSig = crypto
        .createHmac('sha256', this.apiSecret)
        .update(body)
        .digest('hex');

      return crypto.timingSafeEqual(
        Buffer.from(token),
        Buffer.from(expectedSig),
      );
    } catch {
      return false;
    }
  }

  /**
   * Builds the NCCO actions for a call that connects to the voice AI WebSocket.
   */
  private buildCallNcco(options: {
    agentId: string;
    tenantId: string;
    callUuid?: string;
    recordingDisclosure?: string;
  }): NccoAction[] {
    const wsUrl = this.apiBaseUrl
      .replace('https://', 'wss://')
      .replace('http://', 'ws://');

    const actions: NccoAction[] = [];

    if (options.recordingDisclosure) {
      actions.push({
        action: 'talk',
        text: options.recordingDisclosure,
        language: 'en-US',
        style: 0,
      });
    }

    actions.push({
      action: 'connect',
      endpoint: [
        {
          type: 'websocket',
          uri: `${wsUrl}/api/v1/telephony/vonage/audio-stream`,
          'content-type': 'audio/l16;rate=16000',
          headers: {
            agentId: options.agentId,
            tenantId: options.tenantId,
            callUuid: options.callUuid || '',
          },
        } as VonageWebSocketEndpoint,
      ],
    });

    return actions;
  }

  /**
   * Generates a JWT token for Vonage API authentication.
   */
  private generateJwt(): string {
    if (!this.applicationId || !this.privateKey) {
      this.logger.warn('Vonage applicationId or privateKey not configured');
      return '';
    }

    const now = Math.floor(Date.now() / 1000);
    const header = { alg: 'RS256', typ: 'JWT' };
    const payload = {
      application_id: this.applicationId,
      iat: now,
      exp: now + 900, // 15 minutes
      jti: crypto.randomUUID(),
    };

    const encodedHeader = Buffer.from(JSON.stringify(header)).toString('base64url');
    const encodedPayload = Buffer.from(JSON.stringify(payload)).toString('base64url');
    const signingInput = `${encodedHeader}.${encodedPayload}`;

    try {
      const sign = crypto.createSign('RSA-SHA256');
      sign.update(signingInput);
      const signature = sign.sign(this.privateKey, 'base64url');
      return `${signingInput}.${signature}`;
    } catch (error) {
      this.logger.error(`Failed to generate Vonage JWT: ${(error as Error).message}`);
      return '';
    }
  }

  /**
   * Maps Vonage call statuses to our internal status format.
   */
  private mapVonageStatus(vonageStatus: string): string {
    const statusMap: Record<string, string> = {
      started: 'initiated',
      ringing: 'ringing',
      answered: 'in-progress',
      machine: 'in-progress',
      completed: 'completed',
      busy: 'busy',
      cancelled: 'cancelled',
      failed: 'failed',
      rejected: 'failed',
      timeout: 'no-answer',
      unanswered: 'no-answer',
    };
    return statusMap[vonageStatus] || vonageStatus;
  }
}
