import {
  Controller,
  Post,
  Req,
  Res,
  Headers,
  Body,
  Logger,
  HttpStatus,
  BadRequestException,
} from '@nestjs/common';
import { ApiTags, ApiOperation, ApiExcludeEndpoint } from '@nestjs/swagger';
import { Request, Response } from 'express';
import { ConfigService } from '@nestjs/config';
import { TwilioMediaStreamProvider } from './providers/twilio-media-stream.provider';
import { VonageVoiceProvider } from './providers/vonage-voice.provider';
import { RedisService } from '../../common/services/redis.service';

/**
 * Telephony webhook controller handling incoming requests from Twilio and Vonage.
 * These endpoints are NOT protected by JWT auth guards since they receive
 * webhooks directly from telephony providers with their own signature validation.
 */
@ApiTags('telephony-webhooks')
@Controller('api/v1/telephony')
export class TelephonyWebhookController {
  private readonly logger = new Logger(TelephonyWebhookController.name);
  private readonly apiBaseUrl: string;

  constructor(
    private readonly twilioMediaStream: TwilioMediaStreamProvider,
    private readonly vonageVoice: VonageVoiceProvider,
    private readonly redisService: RedisService,
    private readonly configService: ConfigService,
  ) {
    this.apiBaseUrl = this.configService.get<string>('API_BASE_URL', 'https://api.example.com');
  }

  // ─── TWILIO WEBHOOKS ─────────────────────────────────────────────────────────

  /**
   * Twilio Voice webhook - called when an incoming call arrives.
   * Returns TwiML that connects the call to a WebSocket media stream.
   */
  @Post('twilio/voice')
  @ApiOperation({ summary: 'Twilio voice webhook for incoming calls' })
  async handleTwilioVoice(
    @Req() req: Request,
    @Res() res: Response,
    @Headers('x-twilio-signature') signature: string,
    @Body() body: Record<string, string>,
  ): Promise<void> {
    this.logger.log(`Twilio voice webhook received: CallSid=${body.CallSid}, From=${body.From}, To=${body.To}`);

    // Validate Twilio signature
    const webhookUrl = `${this.apiBaseUrl}/api/v1/telephony/twilio/voice`;
    if (signature && !this.twilioMediaStream.validateSignature(webhookUrl, body, signature)) {
      this.logger.warn(`Invalid Twilio signature for call ${body.CallSid}`);
      res.status(HttpStatus.FORBIDDEN).send('Invalid signature');
      return;
    }

    // Look up agent and tenant configuration for this phone number
    const routingConfig = await this.resolveInboundRouting(body.To, body.From);

    if (!routingConfig) {
      this.logger.warn(`No routing configuration found for number ${body.To}`);
      const twiml = `<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say>We're sorry, this number is not currently configured. Please try again later.</Say>
  <Hangup/>
</Response>`;
      res.type('text/xml').status(HttpStatus.OK).send(twiml);
      return;
    }

    // Store call metadata in Redis for the media stream handler to pick up
    await this.redisService.setJson(
      `call:incoming:${body.CallSid}`,
      {
        callSid: body.CallSid,
        from: body.From,
        to: body.To,
        direction: body.Direction || 'inbound',
        agentId: routingConfig.agentId,
        tenantId: routingConfig.tenantId,
        receivedAt: new Date().toISOString(),
      },
      3600, // 1 hour TTL
    );

    // Generate TwiML to connect the call to a WebSocket media stream
    const twiml = this.twilioMediaStream.generateMediaStreamTwiml({
      callSid: body.CallSid,
      agentId: routingConfig.agentId,
      tenantId: routingConfig.tenantId,
      initialGreeting: routingConfig.greeting,
    });

    this.logger.log(`Routing call ${body.CallSid} to agent ${routingConfig.agentId} for tenant ${routingConfig.tenantId}`);
    res.type('text/xml').status(HttpStatus.OK).send(twiml);
  }

  /**
   * Twilio status callback webhook - receives call status updates.
   */
  @Post('twilio/status')
  @ApiOperation({ summary: 'Twilio call status callback' })
  async handleTwilioStatus(
    @Req() req: Request,
    @Res() res: Response,
    @Headers('x-twilio-signature') signature: string,
    @Body() body: Record<string, string>,
  ): Promise<void> {
    this.logger.log(
      `Twilio status callback: CallSid=${body.CallSid}, Status=${body.CallStatus}, Duration=${body.CallDuration || 'N/A'}`,
    );

    // Validate Twilio signature
    const webhookUrl = `${this.apiBaseUrl}/api/v1/telephony/twilio/status`;
    if (signature && !this.twilioMediaStream.validateSignature(webhookUrl, body, signature)) {
      this.logger.warn(`Invalid Twilio signature for status callback: ${body.CallSid}`);
      res.status(HttpStatus.FORBIDDEN).send('Invalid signature');
      return;
    }

    // Store status update in Redis for the call orchestrator to consume
    const statusData = {
      callSid: body.CallSid,
      status: body.CallStatus,
      duration: body.CallDuration ? parseInt(body.CallDuration, 10) : undefined,
      from: body.From,
      to: body.To,
      direction: body.Direction,
      timestamp: body.Timestamp || new Date().toISOString(),
      sipResponseCode: body.SipResponseCode,
      errorCode: body.ErrorCode,
      errorMessage: body.ErrorMessage,
    };

    await this.redisService.setJson(
      `call:status:${body.CallSid}`,
      statusData,
      7200, // 2 hours TTL
    );

    // Publish status event for real-time processing
    const redisClient = this.redisService.getClient();
    await redisClient.publish(
      'call:status:update',
      JSON.stringify(statusData),
    );

    // Handle terminal statuses
    const terminalStatuses = ['completed', 'busy', 'no-answer', 'canceled', 'failed'];
    if (terminalStatuses.includes(body.CallStatus)) {
      this.logger.log(`Call ${body.CallSid} reached terminal status: ${body.CallStatus}`);
      await redisClient.publish(
        'call:ended',
        JSON.stringify(statusData),
      );
    }

    res.status(HttpStatus.OK).send('OK');
  }

  /**
   * Twilio Media Stream WebSocket upgrade endpoint.
   * In production, this is handled by the WebSocket server (e.g., via ws library
   * or the call orchestrator gateway). This POST endpoint serves as documentation
   * and fallback; the actual WebSocket connection is negotiated at the protocol level.
   */
  @Post('twilio/media-stream')
  @ApiExcludeEndpoint()
  async handleTwilioMediaStream(
    @Res() res: Response,
  ): Promise<void> {
    // The actual WebSocket upgrade is handled by the NestJS WebSocket adapter
    // or a raw HTTP upgrade handler in the main.ts bootstrap.
    // This POST endpoint returns an informational response.
    this.logger.warn('Media stream POST received - WebSocket upgrade should be handled at protocol level');
    res.status(HttpStatus.OK).json({
      message: 'Media stream endpoint - connect via WebSocket (wss://)',
    });
  }

  // ─── VONAGE WEBHOOKS ──────────────────────────────────────────────────────────

  /**
   * Vonage answer webhook - called when an incoming call is answered.
   * Returns NCCO actions to control the call flow.
   */
  @Post('vonage/answer')
  @ApiOperation({ summary: 'Vonage answer webhook for incoming calls' })
  async handleVonageAnswer(
    @Req() req: Request,
    @Res() res: Response,
    @Body() body: Record<string, string>,
  ): Promise<void> {
    const callUuid = body.uuid || body.conversation_uuid;
    const fromNumber = body.from;
    const toNumber = body.to;

    this.logger.log(`Vonage answer webhook: uuid=${callUuid}, from=${fromNumber}, to=${toNumber}`);

    // Look up agent and tenant for this phone number
    const routingConfig = await this.resolveInboundRouting(toNumber, fromNumber);

    if (!routingConfig) {
      this.logger.warn(`No routing configuration found for Vonage number ${toNumber}`);
      const ncco = [
        {
          action: 'talk',
          text: 'We are sorry, this number is not currently configured. Please try again later.',
          language: 'en-US',
        },
      ];
      res.status(HttpStatus.OK).json(ncco);
      return;
    }

    // Store call metadata in Redis
    await this.redisService.setJson(
      `call:incoming:vonage:${callUuid}`,
      {
        callUuid,
        from: fromNumber,
        to: toNumber,
        direction: 'inbound',
        agentId: routingConfig.agentId,
        tenantId: routingConfig.tenantId,
        provider: 'vonage',
        receivedAt: new Date().toISOString(),
      },
      3600,
    );

    // Generate NCCO to connect to WebSocket for voice AI processing
    const ncco = this.vonageVoice.generateAnswerNcco({
      agentId: routingConfig.agentId,
      tenantId: routingConfig.tenantId,
      callUuid,
      recordingDisclosure: routingConfig.greeting,
    });

    this.logger.log(`Vonage call ${callUuid} routed to agent ${routingConfig.agentId}`);
    res.status(HttpStatus.OK).json(ncco);
  }

  /**
   * Vonage event webhook - receives call status and event updates.
   */
  @Post('vonage/event')
  @ApiOperation({ summary: 'Vonage event webhook' })
  async handleVonageEvent(
    @Req() req: Request,
    @Res() res: Response,
    @Headers('authorization') authorization: string,
    @Body() body: Record<string, unknown>,
  ): Promise<void> {
    const event = this.vonageVoice.parseEventWebhook(body);

    this.logger.log(
      `Vonage event: uuid=${event.uuid}, status=${event.status}, direction=${event.direction}`,
    );

    // Validate Vonage webhook if authorization header is present
    if (authorization) {
      const token = authorization.replace('Bearer ', '');
      const rawBody = JSON.stringify(body);
      if (!this.vonageVoice.validateWebhookSignature(token, rawBody)) {
        this.logger.warn(`Invalid Vonage webhook signature for event: ${event.uuid}`);
        res.status(HttpStatus.FORBIDDEN).send('Invalid signature');
        return;
      }
    }

    // Store event in Redis
    const eventData = {
      callSid: event.uuid,
      conversationUuid: event.conversation_uuid,
      status: event.status,
      direction: event.direction,
      timestamp: event.timestamp,
      from: event.from,
      to: event.to,
      duration: event.duration ? parseInt(event.duration, 10) : undefined,
      provider: 'vonage',
    };

    await this.redisService.setJson(
      `call:status:vonage:${event.uuid}`,
      eventData,
      7200,
    );

    // Publish event for real-time processing
    const redisClient = this.redisService.getClient();
    await redisClient.publish(
      'call:status:update',
      JSON.stringify(eventData),
    );

    // Handle terminal Vonage statuses
    const terminalStatuses = ['completed', 'busy', 'cancelled', 'failed', 'rejected', 'timeout', 'unanswered'];
    if (terminalStatuses.includes(event.status)) {
      this.logger.log(`Vonage call ${event.uuid} reached terminal status: ${event.status}`);
      await redisClient.publish('call:ended', JSON.stringify(eventData));
    }

    res.status(HttpStatus.OK).send('OK');
  }

  // ─── PRIVATE HELPERS ──────────────────────────────────────────────────────────

  /**
   * Resolves inbound routing configuration for a phone number.
   * Looks up the tenant and agent associated with the called number.
   */
  private async resolveInboundRouting(
    toNumber: string,
    fromNumber: string,
  ): Promise<{
    tenantId: string;
    agentId: string;
    greeting?: string;
  } | null> {
    // Normalize phone number (strip + prefix, spaces, dashes)
    const normalizedTo = toNumber?.replace(/[\s\-\+\(\)]/g, '') || '';

    // Check Redis cache first
    const cacheKey = `routing:number:${normalizedTo}`;
    const cached = await this.redisService.getJson<{
      tenantId: string;
      agentId: string;
      greeting?: string;
    }>(cacheKey);

    if (cached) {
      return cached;
    }

    // In a full implementation, this would query the database for the phone number
    // mapping. For now, check Redis for a configured routing entry.
    const configKey = `config:phone:${normalizedTo}`;
    const config = await this.redisService.getJson<{
      tenantId: string;
      agentId: string;
      greeting?: string;
    }>(configKey);

    if (config) {
      // Cache the resolved routing for 5 minutes
      await this.redisService.setJson(cacheKey, config, 300);
      return config;
    }

    this.logger.warn(`No routing config found for number ${normalizedTo} (from: ${fromNumber})`);
    return null;
  }
}
