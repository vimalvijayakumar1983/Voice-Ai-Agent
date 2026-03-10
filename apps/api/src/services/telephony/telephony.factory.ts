import { Injectable, BadRequestException } from '@nestjs/common';
import { ITelephonyProvider } from './telephony.interface';
import { TwilioProvider } from './providers/twilio.provider';
import { PlivoProvider } from './providers/plivo.provider';
import { SIPTrunkProvider } from './providers/sip-trunk.provider';

export type TelephonyProviderType = 'twilio' | 'plivo' | 'sip';

@Injectable()
export class TelephonyFactory {
  private readonly providers = new Map<string, ITelephonyProvider>();

  constructor(
    private readonly twilioProvider: TwilioProvider,
    private readonly plivoProvider: PlivoProvider,
    private readonly sipTrunkProvider: SIPTrunkProvider,
  ) {
    this.providers.set('twilio', twilioProvider);
    this.providers.set('plivo', plivoProvider);
    this.providers.set('sip', sipTrunkProvider);
  }

  getProvider(type: TelephonyProviderType): ITelephonyProvider {
    const provider = this.providers.get(type);
    if (!provider) {
      throw new BadRequestException(`Unsupported telephony provider: ${type}`);
    }
    return provider;
  }

  getAvailableProviders(): string[] {
    return Array.from(this.providers.keys());
  }
}
