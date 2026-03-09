import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { TelephonyFactory, TelephonyProviderType } from './telephony.factory';
import { CallOptions, CallResult, CallStatus } from './telephony.interface';

@Injectable()
export class TelephonyService {
  private readonly logger = new Logger(TelephonyService.name);
  private readonly defaultProvider: TelephonyProviderType;

  constructor(
    private readonly factory: TelephonyFactory,
    private readonly configService: ConfigService,
  ) {
    this.defaultProvider = this.configService.get<TelephonyProviderType>(
      'TELEPHONY_PROVIDER',
      'twilio',
    );
  }

  async makeCall(options: CallOptions, providerType?: TelephonyProviderType): Promise<CallResult> {
    const provider = this.factory.getProvider(providerType || this.defaultProvider);
    this.logger.log(`Making call via ${provider.providerName} to ${options.to}`);
    return provider.makeCall(options);
  }

  async endCall(callSid: string, providerType?: TelephonyProviderType): Promise<void> {
    const provider = this.factory.getProvider(providerType || this.defaultProvider);
    return provider.endCall(callSid);
  }

  async transferCall(
    callSid: string,
    targetNumber: string,
    providerType?: TelephonyProviderType,
  ): Promise<void> {
    const provider = this.factory.getProvider(providerType || this.defaultProvider);
    return provider.transferCall(callSid, targetNumber);
  }

  async getCallStatus(callSid: string, providerType?: TelephonyProviderType): Promise<CallStatus> {
    const provider = this.factory.getProvider(providerType || this.defaultProvider);
    return provider.getCallStatus(callSid);
  }

  async sendDTMF(callSid: string, digits: string, providerType?: TelephonyProviderType): Promise<void> {
    const provider = this.factory.getProvider(providerType || this.defaultProvider);
    return provider.sendDTMF(callSid, digits);
  }
}
