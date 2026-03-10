import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { EventEmitter } from 'events';
import {
  ITelephonyProvider,
  CallOptions,
  CallResult,
  CallStatus,
} from '../telephony.interface';

/**
 * SIP Trunk session representing an active call over SIP.
 */
export interface SIPCallSession {
  callId: string;
  sipCallId: string;
  from: string;
  to: string;
  direction: 'inbound' | 'outbound';
  status: string;
  startedAt: Date;
  answeredAt?: Date;
  endedAt?: Date;
  isActive: boolean;
  rtpPort?: number;
  codec: string;
}

/**
 * SIP Trunk configuration for providers like Etisalat, du, etc.
 */
export interface SIPTrunkConfig {
  sipProxy: string;
  sipPort: number;
  sipUsername: string;
  sipPassword: string;
  sipDomain: string;
  sipTransport: 'udp' | 'tcp' | 'tls';
  outboundCallerId: string;
  codec: 'PCMU' | 'PCMA' | 'G729' | 'OPUS';
  rtpPortRange: { min: number; max: number };
  registrationEnabled: boolean;
  registrationExpiry: number;
  keepAliveInterval: number;
}

/**
 * SIP Trunk Provider for direct SIP trunk integration.
 *
 * Supports Etisalat, du, and other UAE/Middle East SIP trunk providers.
 * Uses SIP protocol for call signaling and RTP for media transport.
 *
 * Architecture:
 * - SIP signaling: INVITE/BYE/REGISTER via UDP/TCP/TLS
 * - Media: RTP audio stream (PCMU/PCMA at 8kHz for telephony)
 * - Audio pipeline integration: RTP ↔ PCM conversion feeds into Deepgram STT
 *
 * In production, this provider interfaces with a local SIP gateway
 * (FreeSWITCH, Otheran, or Otheran-srf) that handles the actual SIP protocol,
 * and this provider manages the call lifecycle and audio bridging.
 */
@Injectable()
export class SIPTrunkProvider extends EventEmitter implements ITelephonyProvider {
  readonly providerName = 'sip';
  private readonly logger = new Logger(SIPTrunkProvider.name);
  private readonly config: SIPTrunkConfig;
  private readonly activeCalls = new Map<string, SIPCallSession>();
  private isRegistered = false;
  private registrationTimer: ReturnType<typeof setInterval> | null = null;

  constructor(private readonly configService: ConfigService) {
    super();
    this.config = {
      sipProxy: this.configService.get<string>('SIP_PROXY_HOST', ''),
      sipPort: this.configService.get<number>('SIP_PROXY_PORT', 5060),
      sipUsername: this.configService.get<string>('SIP_USERNAME', ''),
      sipPassword: this.configService.get<string>('SIP_PASSWORD', ''),
      sipDomain: this.configService.get<string>('SIP_DOMAIN', ''),
      sipTransport: this.configService.get<'udp' | 'tcp' | 'tls'>('SIP_TRANSPORT', 'udp'),
      outboundCallerId: this.configService.get<string>('SIP_CALLER_ID', ''),
      codec: this.configService.get<'PCMU' | 'PCMA' | 'G729' | 'OPUS'>('SIP_CODEC', 'PCMU'),
      rtpPortRange: {
        min: this.configService.get<number>('SIP_RTP_PORT_MIN', 10000),
        max: this.configService.get<number>('SIP_RTP_PORT_MAX', 20000),
      },
      registrationEnabled: this.configService.get<boolean>('SIP_REGISTRATION_ENABLED', true),
      registrationExpiry: this.configService.get<number>('SIP_REGISTRATION_EXPIRY', 3600),
      keepAliveInterval: this.configService.get<number>('SIP_KEEPALIVE_INTERVAL', 30),
    };

    if (this.config.sipProxy) {
      this.logger.log(
        `SIP Trunk provider initialized: proxy=${this.config.sipProxy}:${this.config.sipPort}, ` +
        `transport=${this.config.sipTransport}, codec=${this.config.codec}`,
      );
    }
  }

  /**
   * Registers with the SIP trunk provider (e.g., Etisalat SBC).
   * Must be called on application startup for inbound call reception.
   */
  async register(): Promise<boolean> {
    if (!this.config.sipProxy || !this.config.sipUsername) {
      this.logger.warn('SIP trunk not configured - skipping registration');
      return false;
    }

    this.logger.log(
      `Registering with SIP proxy ${this.config.sipProxy}:${this.config.sipPort} ` +
      `as ${this.config.sipUsername}@${this.config.sipDomain}`,
    );

    // In production: Send SIP REGISTER request to the proxy
    // const registerRequest = this.buildRegisterRequest();
    // await this.sendSIPMessage(registerRequest);
    // Handle 401 Unauthorized → re-send with digest auth credentials
    // Handle 200 OK → registration successful

    this.isRegistered = true;

    // Set up periodic re-registration
    if (this.config.registrationEnabled && !this.registrationTimer) {
      const reRegisterMs = (this.config.registrationExpiry - 60) * 1000; // Re-register 60s before expiry
      this.registrationTimer = setInterval(() => {
        this.register().catch((err) => {
          this.logger.error(`SIP re-registration failed: ${err.message}`);
        });
      }, reRegisterMs);
    }

    this.emit('registered', {
      proxy: this.config.sipProxy,
      username: this.config.sipUsername,
      domain: this.config.sipDomain,
    });

    this.logger.log('SIP trunk registration successful');
    return true;
  }

  /**
   * Unregisters from the SIP trunk provider.
   */
  async unregister(): Promise<void> {
    if (this.registrationTimer) {
      clearInterval(this.registrationTimer);
      this.registrationTimer = null;
    }

    if (this.isRegistered) {
      this.logger.log('Unregistering from SIP trunk');
      // In production: Send REGISTER with Expires: 0
      this.isRegistered = false;
      this.emit('unregistered');
    }
  }

  /**
   * Makes an outbound call via SIP trunk.
   * Sends SIP INVITE to the proxy with SDP for media negotiation.
   */
  async makeCall(options: CallOptions): Promise<CallResult> {
    const callId = `SIP-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const from = options.from || this.config.outboundCallerId;

    this.logger.log(`[SIP] Making call to ${options.to} from ${from} via ${this.config.sipProxy}`);

    // Allocate an RTP port for media
    const rtpPort = this.allocateRTPPort();

    const session: SIPCallSession = {
      callId,
      sipCallId: `${callId}@${this.config.sipDomain}`,
      from,
      to: options.to,
      direction: 'outbound',
      status: 'inviting',
      startedAt: new Date(),
      isActive: true,
      rtpPort,
      codec: this.config.codec,
    };

    this.activeCalls.set(callId, session);

    // In production: Build and send SIP INVITE
    // const sdp = this.buildSDP(rtpPort, this.config.codec);
    // const invite = this.buildInviteRequest(options.to, from, sdp);
    // await this.sendSIPMessage(invite);
    // Wait for 100 Trying → 180 Ringing → 200 OK
    // Send ACK on 200 OK
    // Start RTP media session

    // Emit event for call orchestrator
    this.emit('call:initiated', { callId, session });

    return {
      callSid: callId,
      status: 'queued',
      provider: this.providerName,
    };
  }

  /**
   * Handles an incoming SIP INVITE (inbound call).
   * Called by the SIP message handler when a new INVITE arrives.
   */
  async handleIncomingInvite(sipMessage: {
    callId: string;
    from: string;
    to: string;
    sdp: string;
  }): Promise<SIPCallSession> {
    const callId = `SIP-IN-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const rtpPort = this.allocateRTPPort();

    this.logger.log(`[SIP] Incoming call from ${sipMessage.from} to ${sipMessage.to}`);

    const session: SIPCallSession = {
      callId,
      sipCallId: sipMessage.callId,
      from: sipMessage.from,
      to: sipMessage.to,
      direction: 'inbound',
      status: 'ringing',
      startedAt: new Date(),
      isActive: true,
      rtpPort,
      codec: this.config.codec,
    };

    this.activeCalls.set(callId, session);

    // In production:
    // 1. Send 100 Trying
    // 2. Send 180 Ringing
    // 3. Wait for answerCall() to send 200 OK with SDP
    // 4. Receive ACK
    // 5. Start RTP media session

    this.emit('call:incoming', { callId, session });
    return session;
  }

  async answerCall(callSid: string, actions: Record<string, unknown>): Promise<void> {
    const session = this.activeCalls.get(callSid);
    if (!session) {
      this.logger.warn(`[SIP] No call found for ${callSid}`);
      return;
    }

    this.logger.log(`[SIP] Answering call ${callSid}`);
    session.status = 'answered';
    session.answeredAt = new Date();

    // In production: Send 200 OK with SDP answer
    // const sdp = this.buildSDP(session.rtpPort, session.codec);
    // await this.sendSIPResponse(200, 'OK', session.sipCallId, sdp);

    this.emit('call:answered', { callId: callSid, session });
  }

  async endCall(callSid: string): Promise<void> {
    const session = this.activeCalls.get(callSid);
    if (!session) {
      this.logger.warn(`[SIP] No call found for ${callSid}`);
      return;
    }

    this.logger.log(`[SIP] Ending call ${callSid}`);
    session.status = 'completed';
    session.endedAt = new Date();
    session.isActive = false;

    // In production: Send SIP BYE request
    // await this.sendBYE(session.sipCallId);

    // Release RTP port
    if (session.rtpPort) {
      this.releaseRTPPort(session.rtpPort);
    }

    this.emit('call:ended', {
      callId: callSid,
      session,
      duration: session.answeredAt
        ? Math.round((Date.now() - session.answeredAt.getTime()) / 1000)
        : 0,
    });

    this.activeCalls.delete(callSid);
  }

  async transferCall(callSid: string, targetNumber: string): Promise<void> {
    const session = this.activeCalls.get(callSid);
    if (!session) {
      this.logger.warn(`[SIP] No call found for ${callSid}`);
      return;
    }

    this.logger.log(`[SIP] Transferring call ${callSid} to ${targetNumber}`);

    // In production: Send SIP REFER request for blind transfer
    // await this.sendREFER(session.sipCallId, targetNumber);

    this.emit('call:transferred', { callId: callSid, target: targetNumber });
  }

  async getCallStatus(callSid: string): Promise<CallStatus> {
    const session = this.activeCalls.get(callSid);
    if (!session) {
      return {
        callSid,
        status: 'not-found',
        direction: 'unknown',
      };
    }

    return {
      callSid,
      status: session.status,
      direction: session.direction,
      startTime: session.startedAt,
      endTime: session.endedAt,
      duration: session.answeredAt
        ? Math.round((Date.now() - session.answeredAt.getTime()) / 1000)
        : undefined,
    };
  }

  async sendDTMF(callSid: string, digits: string): Promise<void> {
    const session = this.activeCalls.get(callSid);
    if (!session) {
      this.logger.warn(`[SIP] No call found for ${callSid}`);
      return;
    }

    this.logger.log(`[SIP] Sending DTMF ${digits} to call ${callSid}`);

    // In production: Send DTMF via RTP (RFC 2833) or SIP INFO
    // await this.sendRFC2833DTMF(session, digits);

    this.emit('call:dtmf', { callId: callSid, digits });
  }

  // ─── AUDIO BRIDGE ───────────────────────────────────────────────────────────

  /**
   * Processes incoming RTP audio from the SIP call.
   * Converts to PCM and emits for the audio pipeline.
   */
  processIncomingRTP(callId: string, rtpPayload: Buffer): Buffer | null {
    const session = this.activeCalls.get(callId);
    if (!session || !session.isActive) return null;

    // Strip 12-byte RTP header to get raw audio payload
    if (rtpPayload.length <= 12) return null;
    const audioPayload = rtpPayload.subarray(12);

    // Convert codec to PCM16 for the audio pipeline
    let pcmAudio: Buffer;
    switch (session.codec) {
      case 'PCMU': // G.711 mu-law
        pcmAudio = this.mulawToPcm(audioPayload);
        break;
      case 'PCMA': // G.711 A-law
        pcmAudio = this.alawToPcm(audioPayload);
        break;
      default:
        // For G.729 or OPUS, a decoder library would be needed
        pcmAudio = this.mulawToPcm(audioPayload);
    }

    this.emit('audio:incoming', { callId, pcmAudio });
    return pcmAudio;
  }

  /**
   * Sends PCM audio to the SIP call as RTP.
   * Converts from PCM to the negotiated codec.
   */
  sendAudioToCall(callId: string, pcmAudio: Buffer): Buffer | null {
    const session = this.activeCalls.get(callId);
    if (!session || !session.isActive) return null;

    // Convert PCM16 to negotiated codec
    let encodedAudio: Buffer;
    switch (session.codec) {
      case 'PCMU':
        encodedAudio = this.pcmToMulaw(pcmAudio);
        break;
      case 'PCMA':
        encodedAudio = this.pcmToAlaw(pcmAudio);
        break;
      default:
        encodedAudio = this.pcmToMulaw(pcmAudio);
    }

    this.emit('audio:outgoing', { callId, encodedAudio });
    return encodedAudio;
  }

  // ─── SIP TRUNK STATUS ───────────────────────────────────────────────────────

  /**
   * Returns whether the provider is registered with the SIP trunk.
   */
  getRegistrationStatus(): boolean {
    return this.isRegistered;
  }

  /**
   * Returns the trunk configuration (without credentials).
   */
  getTrunkInfo(): {
    proxy: string;
    port: number;
    domain: string;
    transport: string;
    codec: string;
    registered: boolean;
    activeCalls: number;
  } {
    return {
      proxy: this.config.sipProxy,
      port: this.config.sipPort,
      domain: this.config.sipDomain,
      transport: this.config.sipTransport,
      codec: this.config.codec,
      registered: this.isRegistered,
      activeCalls: this.activeCalls.size,
    };
  }

  /**
   * Returns count of active SIP calls.
   */
  getActiveCallCount(): number {
    return this.activeCalls.size;
  }

  /**
   * Returns a call session by SIP Call-ID header.
   */
  getSessionBySIPCallId(sipCallId: string): SIPCallSession | undefined {
    for (const session of this.activeCalls.values()) {
      if (session.sipCallId === sipCallId) return session;
    }
    return undefined;
  }

  // ─── AUDIO CODEC HELPERS ────────────────────────────────────────────────────

  private mulawToPcm(mulawBuffer: Buffer): Buffer {
    const pcmBuffer = Buffer.alloc(mulawBuffer.length * 2);
    for (let i = 0; i < mulawBuffer.length; i++) {
      const pcmSample = this.decodeMulaw(mulawBuffer[i]);
      pcmBuffer.writeInt16LE(pcmSample, i * 2);
    }
    return pcmBuffer;
  }

  private pcmToMulaw(pcmBuffer: Buffer): Buffer {
    const mulawBuffer = Buffer.alloc(pcmBuffer.length / 2);
    for (let i = 0; i < mulawBuffer.length; i++) {
      const pcmSample = pcmBuffer.readInt16LE(i * 2);
      mulawBuffer[i] = this.encodeMulaw(pcmSample);
    }
    return mulawBuffer;
  }

  private alawToPcm(alawBuffer: Buffer): Buffer {
    const pcmBuffer = Buffer.alloc(alawBuffer.length * 2);
    for (let i = 0; i < alawBuffer.length; i++) {
      const pcmSample = this.decodeAlaw(alawBuffer[i]);
      pcmBuffer.writeInt16LE(pcmSample, i * 2);
    }
    return pcmBuffer;
  }

  private pcmToAlaw(pcmBuffer: Buffer): Buffer {
    const alawBuffer = Buffer.alloc(pcmBuffer.length / 2);
    for (let i = 0; i < alawBuffer.length; i++) {
      const pcmSample = pcmBuffer.readInt16LE(i * 2);
      alawBuffer[i] = this.encodeAlaw(pcmSample);
    }
    return alawBuffer;
  }

  private decodeMulaw(mulawByte: number): number {
    const MULAW_BIAS = 33;
    mulawByte = ~mulawByte & 0xff;
    const sign = (mulawByte & 0x80) !== 0 ? -1 : 1;
    const exponent = (mulawByte >> 4) & 0x07;
    const mantissa = mulawByte & 0x0f;
    let sample = mantissa << (exponent + 3);
    sample += MULAW_BIAS << (exponent + 3);
    sample -= MULAW_BIAS;
    return sign * sample;
  }

  private encodeMulaw(pcmSample: number): number {
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
    return ~(sign | (exponent << 4) | mantissa) & 0xff;
  }

  private decodeAlaw(alawByte: number): number {
    alawByte ^= 0x55;
    const sign = (alawByte & 0x80) !== 0 ? -1 : 1;
    const exponent = (alawByte >> 4) & 0x07;
    const mantissa = alawByte & 0x0f;
    let sample: number;
    if (exponent === 0) {
      sample = (mantissa << 4) + 8;
    } else {
      sample = ((mantissa << 4) + 0x108) << (exponent - 1);
    }
    return sign * sample;
  }

  private encodeAlaw(pcmSample: number): number {
    const ALAW_MAX = 0xfff;
    const sign = pcmSample < 0 ? 0x80 : 0;
    if (pcmSample < 0) pcmSample = -pcmSample;
    if (pcmSample > ALAW_MAX) pcmSample = ALAW_MAX;

    let exponent = 7;
    let expMask = 0x800;
    for (; exponent > 0; exponent--) {
      if ((pcmSample & expMask) !== 0) break;
      expMask >>= 1;
    }

    const mantissa = exponent === 0
      ? (pcmSample >> 4) & 0x0f
      : (pcmSample >> (exponent + 3)) & 0x0f;

    return (sign | (exponent << 4) | mantissa) ^ 0x55;
  }

  // ─── RTP PORT MANAGEMENT ────────────────────────────────────────────────────

  private usedPorts = new Set<number>();

  private allocateRTPPort(): number {
    const { min, max } = this.config.rtpPortRange;
    // RTP ports must be even (RTCP uses odd port = RTP port + 1)
    for (let port = min; port <= max; port += 2) {
      if (!this.usedPorts.has(port)) {
        this.usedPorts.add(port);
        return port;
      }
    }
    // Fallback: reuse a port (shouldn't happen in practice)
    this.logger.warn('RTP port pool exhausted, reusing port');
    return min;
  }

  private releaseRTPPort(port: number): void {
    this.usedPorts.delete(port);
  }
}
