import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import {
  ITelephonyProvider,
  CallOptions,
  CallResult,
  CallStatus,
} from '../telephony.interface';

@Injectable()
export class PlivoProvider implements ITelephonyProvider {
  readonly providerName = 'plivo';
  private readonly logger = new Logger(PlivoProvider.name);
  private readonly authId: string;
  private readonly authToken: string;

  constructor(private readonly configService: ConfigService) {
    this.authId = this.configService.get<string>('PLIVO_AUTH_ID', '');
    this.authToken = this.configService.get<string>('PLIVO_AUTH_TOKEN', '');
  }

  async makeCall(options: CallOptions): Promise<CallResult> {
    this.logger.log(`[Plivo] Making call to ${options.to} from ${options.from}`);

    // In production: use Plivo SDK
    // const client = new plivo.Client(this.authId, this.authToken);
    // const response = await client.calls.create(from, to, answerUrl, ...);

    return {
      callSid: `PL-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      status: 'queued',
      provider: this.providerName,
    };
  }

  async answerCall(callSid: string, actions: Record<string, unknown>): Promise<void> {
    this.logger.log(`[Plivo] Answering call ${callSid}`);
  }

  async endCall(callSid: string): Promise<void> {
    this.logger.log(`[Plivo] Ending call ${callSid}`);
  }

  async transferCall(callSid: string, targetNumber: string): Promise<void> {
    this.logger.log(`[Plivo] Transferring call ${callSid} to ${targetNumber}`);
  }

  async getCallStatus(callSid: string): Promise<CallStatus> {
    this.logger.log(`[Plivo] Getting status for call ${callSid}`);
    return {
      callSid,
      status: 'in-progress',
      direction: 'outbound',
    };
  }

  async sendDTMF(callSid: string, digits: string): Promise<void> {
    this.logger.log(`[Plivo] Sending DTMF ${digits} to call ${callSid}`);
  }
}
