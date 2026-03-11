import { Module } from '@nestjs/common';
import { TelephonyFactory } from './telephony.factory';
import { TelephonyService } from './telephony.service';
import { TelephonyWebhookController } from './webhook.controller';
import { TwilioProvider } from './providers/twilio.provider';
import { TwilioMediaStreamProvider } from './providers/twilio-media-stream.provider';
import { PlivoProvider } from './providers/plivo.provider';
import { SIPTrunkProvider } from './providers/sip-trunk.provider';
import { VonageVoiceProvider } from './providers/vonage-voice.provider';
import { RedisService } from '../../common/services/redis.service';

@Module({
  controllers: [TelephonyWebhookController],
  providers: [
    TwilioProvider,
    TwilioMediaStreamProvider,
    PlivoProvider,
    SIPTrunkProvider,
    VonageVoiceProvider,
    TelephonyFactory,
    TelephonyService,
    RedisService,
  ],
  exports: [
    TelephonyFactory,
    TelephonyService,
    VonageVoiceProvider,
    TwilioProvider,
    TwilioMediaStreamProvider,
  ],
})
export class TelephonyModule {}
