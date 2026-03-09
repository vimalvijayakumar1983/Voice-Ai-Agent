import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import {
  ITelephonyProvider,
  CallOptions,
  CallResult,
  CallStatus,
} from '../telephony.interface';

@Injectable()
export class TwilioProvider implements ITelephonyProvider {
  readonly providerName = 'twilio';
  private readonly logger = new Logger(TwilioProvider.name);
  private readonly accountSid: string;
  private readonly authToken: string;

  constructor(private readonly configService: ConfigService) {
    this.accountSid = this.configService.get<string>('TWILIO_ACCOUNT_SID', '');
    this.authToken = this.configService.get<string>('TWILIO_AUTH_TOKEN', '');
  }

  async makeCall(options: CallOptions): Promise<CallResult> {
    this.logger.log(`[Twilio] Making call to ${options.to} from ${options.from}`);

    // In production: use Twilio SDK
    // const client = twilio(this.accountSid, this.authToken);
    // const call = await client.calls.create({ ... });

    return {
      callSid: `TW-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      status: 'queued',
      provider: this.providerName,
    };
  }

  async answerCall(callSid: string, actions: Record<string, unknown>): Promise<void> {
    this.logger.log(`[Twilio] Answering call ${callSid}`);
    // In production: update call with TwiML instructions
  }

  async endCall(callSid: string): Promise<void> {
    this.logger.log(`[Twilio] Ending call ${callSid}`);
    // In production: client.calls(callSid).update({ status: 'completed' });
  }

  async transferCall(callSid: string, targetNumber: string): Promise<void> {
    this.logger.log(`[Twilio] Transferring call ${callSid} to ${targetNumber}`);
    // In production: update call with transfer TwiML
  }

  async getCallStatus(callSid: string): Promise<CallStatus> {
    this.logger.log(`[Twilio] Getting status for call ${callSid}`);

    // In production: const call = await client.calls(callSid).fetch();
    return {
      callSid,
      status: 'in-progress',
      direction: 'outbound',
    };
  }

  async sendDTMF(callSid: string, digits: string): Promise<void> {
    this.logger.log(`[Twilio] Sending DTMF ${digits} to call ${callSid}`);
    // In production: use Twilio API to send DTMF tones
  }
}
