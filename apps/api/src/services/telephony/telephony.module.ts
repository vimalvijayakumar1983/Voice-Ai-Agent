import { Module } from '@nestjs/common';
import { TelephonyFactory } from './telephony.factory';
import { TelephonyService } from './telephony.service';
import { TelephonyWebhookController } from './webhook.controller';
import { TwilioProvider } from './providers/twilio.provider';
import { TwilioMediaStreamProvider } from './providers/twilio-media-stream.provider';
import { PlivoProvider } from './providers/plivo.provider';
import { SIPTrunkProvider } from './providers/sip-trunk.provider';
import { VonageVoiceProvider } from './providers/vonage-voice.provider';
import { VonageAudioStreamGateway } from './providers/vonage-audio-stream.gateway';
import { RedisService } from '../../common/services/redis.service';
import { ConfigService } from '@nestjs/config';

@Module({
  controllers: [TelephonyWebhookController],
  providers: [
    TwilioProvider,
    TwilioMediaStreamProvider,
    PlivoProvider,
    SIPTrunkProvider,
    VonageVoiceProvider,
    {
      provide: VonageAudioStreamGateway,
      useFactory: (configService: ConfigService) => {
        return new VonageAudioStreamGateway(configService);
      },
      inject: [ConfigService],
    },
    TelephonyFactory,
    TelephonyService,
    RedisService,
  ],
  exports: [
    TelephonyFactory,
    TelephonyService,
    VonageVoiceProvider,
    VonageAudioStreamGateway,
    TwilioProvider,
    TwilioMediaStreamProvider,
  ],
})
export class TelephonyModule {}
