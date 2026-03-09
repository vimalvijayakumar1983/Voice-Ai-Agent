import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { PrismaService } from '../../common/services/prisma.service';
import { RedisService } from '../../common/services/redis.service';
import { QueueService } from '../../common/services/queue.service';
import { S3Service } from '../../common/services/s3.service';
import { TelephonyService } from '../../services/telephony/telephony.service';
import { AudioPipelineService, PipelineState } from '../../services/voice/audio-pipeline.service';
import { TwilioMediaStreamProvider } from '../../services/telephony/providers/twilio-media-stream.provider';
import { LLMService } from '../../services/llm/llm.service';
import { LLMMessage } from '../../services/llm/llm.interface';
import { CallStatus } from './dto/call.dto';

export enum CallPhase {
  INITIATED = 'initiated',
  RINGING = 'ringing',
  ANSWERED = 'answered',
  SPEAKING = 'speaking',
  LISTENING = 'listening',
  PROCESSING = 'processing',
  TRANSFERRING = 'transferring',
  ENDED = 'ended',
}

export interface CallSession {
  callId: string;
  tenantId: string;
  agentId: string;
  contactId?: string;
  campaignId?: string;
  callSid: string;
  streamSid?: string;
  phase: CallPhase;
  conversationHistory: LLMMessage[];
  startedAt: Date;
  answeredAt?: Date;
  endedAt?: Date;
  systemPrompt: string;
  voice: string;
  language: string;
  isRecording: boolean;
  recordingChunks: Buffer[];
  metadata: Record<string, unknown>;
}

export interface CallOutcome {
  callId: string;
  status: string;
  durationSeconds: number;
  disposition: string;
  sentiment: string;
  summary: string;
  transcript: LLMMessage[];
  recordingKey?: string;
}

@Injectable()
export class CallOrchestratorService {
  private readonly logger = new Logger(CallOrchestratorService.name);
  private readonly activeSessions = new Map<string, CallSession>();
  private readonly apiBaseUrl: string;

  constructor(
    private readonly prisma: PrismaService,
    private readonly redisService: RedisService,
    private readonly queueService: QueueService,
    private readonly s3Service: S3Service,
    private readonly configService: ConfigService,
    private readonly telephonyService: TelephonyService,
    private readonly audioPipeline: AudioPipelineService,
    private readonly twilioMediaStream: TwilioMediaStreamProvider,
    private readonly llmService: LLMService,
  ) {
    this.apiBaseUrl = this.configService.get<string>('API_BASE_URL', 'https://api.example.com');
    this.setupPipelineListeners();
  }

  /**
   * Initiates a new outbound call and sets up the full orchestration pipeline.
   */
  async initiateCall(params: {
    callId: string;
    tenantId: string;
    agentId: string;
    contactId?: string;
    campaignId?: string;
    toNumber: string;
    fromNumber: string;
  }): Promise<CallSession> {
    const { callId, tenantId, agentId } = params;

    this.logger.log(`Initiating call ${callId} to ${params.toNumber}`);

    // Load agent configuration
    const agent = await this.prisma.agent.findFirst({
      where: { id: agentId, tenantId, isActive: true },
    });

    if (!agent) {
      throw new Error(`Agent ${agentId} not found or inactive`);
    }

    const agentConfig = (agent.config as Record<string, unknown>) || {};

    // Check compliance: consent and recording disclosure
    if (params.contactId) {
      await this.checkCompliance(tenantId, params.contactId);
    }

    // Create the call session
    const session: CallSession = {
      callId,
      tenantId,
      agentId,
      contactId: params.contactId,
      campaignId: params.campaignId,
      callSid: '',
      phase: CallPhase.INITIATED,
      conversationHistory: [],
      startedAt: new Date(),
      systemPrompt: (agentConfig.systemPrompt as string) || agent.description || '',
      voice: (agentConfig.voice as string) || 'default',
      language: (agentConfig.language as string) || 'en-US',
      isRecording: (agentConfig.enableRecording as boolean) !== false,
      recordingChunks: [],
      metadata: {
        contactId: params.contactId,
        campaignId: params.campaignId,
        toNumber: params.toNumber,
        fromNumber: params.fromNumber,
      },
    };

    this.activeSessions.set(callId, session);

    // Initiate the telephony call
    try {
      const callResult = await this.telephonyService.makeCall({
        to: params.toNumber,
        from: params.fromNumber,
        callbackUrl: `${this.apiBaseUrl}/api/v1/telephony/twilio/voice`,
        statusCallbackUrl: `${this.apiBaseUrl}/api/v1/telephony/twilio/status`,
        record: session.isRecording,
        metadata: {
          callId,
          tenantId,
          agentId,
        },
      });

      session.callSid = callResult.callSid;

      // Update call record in database
      await this.prisma.call.update({
        where: { id: callId },
        data: {
          externalCallId: callResult.callSid,
          status: CallStatus.RINGING,
        },
      });

      this.updatePhase(callId, CallPhase.RINGING);

      // Store session in Redis for cross-process access
      await this.storeSessionInRedis(session);

      this.logger.log(`Call ${callId} initiated, callSid: ${callResult.callSid}`);
      return session;
    } catch (error) {
      this.activeSessions.delete(callId);
      this.logger.error(`Failed to initiate call ${callId}: ${(error as Error).message}`);

      await this.prisma.call.update({
        where: { id: callId },
        data: { status: CallStatus.FAILED },
      });

      throw error;
    }
  }

  /**
   * Handles a call being answered - sets up the audio pipeline.
   */
  async handleCallAnswered(callId: string, streamSid: string): Promise<void> {
    const session = this.activeSessions.get(callId);
    if (!session) {
      this.logger.warn(`Call answered but no session found for ${callId}`);
      return;
    }

    session.streamSid = streamSid;
    session.answeredAt = new Date();
    this.updatePhase(callId, CallPhase.ANSWERED);

    this.logger.log(`Call ${callId} answered, setting up audio pipeline`);

    // Update database
    await this.prisma.call.update({
      where: { id: callId },
      data: {
        status: CallStatus.IN_PROGRESS,
        answeredAt: session.answeredAt,
      },
    });

    // Start the audio pipeline
    await this.audioPipeline.startSession({
      callId,
      tenantId: session.tenantId,
      agentId: session.agentId,
      systemPrompt: session.systemPrompt,
      voice: session.voice,
      language: session.language,
      bargeInEnabled: true,
      silenceTimeoutMs: 15000,
      maxCallDurationMs: 1800000, // 30 minutes
    });

    this.audioPipeline.setStreamSid(callId, streamSid);
  }

  /**
   * Handles incoming audio from the telephony provider.
   */
  handleIncomingAudio(callId: string, pcmAudio: Buffer): void {
    const session = this.activeSessions.get(callId);
    if (!session || session.phase === CallPhase.ENDED) return;

    // Store recording data
    if (session.isRecording) {
      session.recordingChunks.push(pcmAudio);
    }

    // Forward to audio pipeline for STT processing
    this.audioPipeline.processIncomingAudio(callId, pcmAudio);
  }

  /**
   * Handles a request to transfer the call to a human agent.
   */
  async handleHumanHandoff(
    callId: string,
    targetNumber: string,
    reason?: string,
  ): Promise<void> {
    const session = this.activeSessions.get(callId);
    if (!session) return;

    this.logger.log(`Human handoff for call ${callId} to ${targetNumber}, reason: ${reason}`);

    this.updatePhase(callId, CallPhase.TRANSFERRING);

    try {
      await this.telephonyService.transferCall(session.callSid, targetNumber);

      await this.prisma.call.update({
        where: { id: callId },
        data: {
          metadata: {
            ...session.metadata,
            handoffTo: targetNumber,
            handoffReason: reason,
            handoffAt: new Date().toISOString(),
          },
        },
      });
    } catch (error) {
      this.logger.error(`Failed to transfer call ${callId}: ${(error as Error).message}`);
    }
  }

  /**
   * Ends a call and initiates post-call processing.
   */
  async endCall(callId: string, reason?: string): Promise<void> {
    const session = this.activeSessions.get(callId);
    if (!session) {
      this.logger.warn(`No session found for call ${callId} to end`);
      return;
    }

    if (session.phase === CallPhase.ENDED) return;

    this.logger.log(`Ending call ${callId}, reason: ${reason || 'normal'}`);

    this.updatePhase(callId, CallPhase.ENDED);
    session.endedAt = new Date();

    // End audio pipeline
    const pipelineResult = await this.audioPipeline.endSession(callId);

    // End telephony call
    try {
      await this.telephonyService.endCall(session.callSid);
    } catch (error) {
      this.logger.warn(`Error ending telephony call ${session.callSid}: ${(error as Error).message}`);
    }

    // Calculate duration
    const durationSeconds = session.answeredAt
      ? Math.round((session.endedAt.getTime() - session.answeredAt.getTime()) / 1000)
      : 0;

    // Update call record
    await this.prisma.call.update({
      where: { id: callId },
      data: {
        status: CallStatus.COMPLETED,
        endedAt: session.endedAt,
        durationSeconds,
      },
    });

    // Queue post-call processing
    await this.queueService.addJob('call-processing', 'post-call', {
      tenantId: session.tenantId,
      callId,
      campaignId: session.campaignId,
      contactId: session.contactId,
      durationSeconds,
      conversationHistory: pipelineResult?.conversationHistory || session.conversationHistory,
      metrics: pipelineResult?.metrics,
      hasRecording: session.isRecording && session.recordingChunks.length > 0,
    });

    // Clean up
    await this.redisService.del(`call:session:${callId}`);
    this.activeSessions.delete(callId);

    this.logger.log(
      `Call ${callId} ended. Duration: ${durationSeconds}s, ` +
      `Turns: ${pipelineResult?.metrics?.turnCount || 0}`,
    );
  }

  /**
   * Gets the current session for a call.
   */
  getSession(callId: string): CallSession | undefined {
    return this.activeSessions.get(callId);
  }

  /**
   * Gets a session by telephony call SID (e.g., Twilio CallSid).
   */
  getSessionByCallSid(callSid: string): CallSession | undefined {
    for (const session of this.activeSessions.values()) {
      if (session.callSid === callSid) return session;
    }
    return undefined;
  }

  /**
   * Returns all active call sessions for a tenant.
   */
  getActiveSessionsForTenant(tenantId: string): CallSession[] {
    const sessions: CallSession[] = [];
    for (const session of this.activeSessions.values()) {
      if (session.tenantId === tenantId && session.phase !== CallPhase.ENDED) {
        sessions.push(session);
      }
    }
    return sessions;
  }

  /**
   * Returns the count of active calls.
   */
  getActiveCallCount(): number {
    return this.activeSessions.size;
  }

  // ─── PRIVATE METHODS ──────────────────────────────────────────────────────────

  /**
   * Sets up event listeners on the audio pipeline to coordinate call phases.
   */
  private setupPipelineListeners(): void {
    this.audioPipeline.on('pipeline:stateChange', (data: {
      callId: string;
      previousState: PipelineState;
      newState: PipelineState;
    }) => {
      const session = this.activeSessions.get(data.callId);
      if (!session) return;

      switch (data.newState) {
        case PipelineState.LISTENING:
          this.updatePhase(data.callId, CallPhase.LISTENING);
          break;
        case PipelineState.PROCESSING:
          this.updatePhase(data.callId, CallPhase.PROCESSING);
          break;
        case PipelineState.SPEAKING:
          this.updatePhase(data.callId, CallPhase.SPEAKING);
          break;
      }
    });

    this.audioPipeline.on('pipeline:transcript', (data: {
      callId: string;
      text: string;
      isFinal: boolean;
      speaker: string;
    }) => {
      // Emit transcript event for real-time monitoring
      this.emitCallEvent(data.callId, 'transcript', {
        text: data.text,
        isFinal: data.isFinal,
        speaker: data.speaker,
        timestamp: new Date().toISOString(),
      });
    });

    this.audioPipeline.on('pipeline:assistantMessage', (data: {
      callId: string;
      text: string;
    }) => {
      this.emitCallEvent(data.callId, 'transcript', {
        text: data.text,
        isFinal: true,
        speaker: 'ai',
        timestamp: new Date().toISOString(),
      });
    });

    this.audioPipeline.on('pipeline:audioOut', (data: {
      callId: string;
      audio: Buffer;
      isFinal: boolean;
    }) => {
      this.sendAudioToTelephony(data.callId, data.audio);
    });

    this.audioPipeline.on('pipeline:bargeIn', (data: { callId: string }) => {
      this.handleBargeInEvent(data.callId);
    });

    this.audioPipeline.on('pipeline:maxDuration', (data: { callId: string }) => {
      this.endCall(data.callId, 'max_duration_reached');
    });

    this.audioPipeline.on('pipeline:error', (data: {
      callId: string;
      error: string;
      stage: string;
    }) => {
      this.logger.error(`Pipeline error for call ${data.callId} at ${data.stage}: ${data.error}`);
      this.emitCallEvent(data.callId, 'error', {
        error: data.error,
        stage: data.stage,
      });
    });
  }

  /**
   * Sends synthesized audio back to the telephony provider.
   */
  private sendAudioToTelephony(callId: string, pcmAudio: Buffer): void {
    const session = this.activeSessions.get(callId);
    if (!session?.streamSid) return;

    const mediaMessage = this.twilioMediaStream.createMediaMessage(session.streamSid, pcmAudio);

    // Emit the media message for the WebSocket handler to send
    this.emitCallEvent(callId, 'media:outgoing', {
      message: mediaMessage,
      streamSid: session.streamSid,
    });
  }

  /**
   * Handles barge-in by clearing audio in the telephony pipeline.
   */
  private handleBargeInEvent(callId: string): void {
    const session = this.activeSessions.get(callId);
    if (!session?.streamSid) return;

    const clearMessage = this.twilioMediaStream.createClearMessage(session.streamSid);
    this.emitCallEvent(callId, 'media:clear', {
      message: clearMessage,
      streamSid: session.streamSid,
    });
  }

  /**
   * Updates the call phase and emits a phase change event.
   */
  private updatePhase(callId: string, phase: CallPhase): void {
    const session = this.activeSessions.get(callId);
    if (!session) return;

    const previousPhase = session.phase;
    session.phase = phase;

    this.emitCallEvent(callId, 'phase_change', {
      previousPhase,
      newPhase: phase,
      timestamp: new Date().toISOString(),
    });
  }

  /**
   * Emits a call event to Redis pub/sub for real-time monitoring.
   */
  private emitCallEvent(
    callId: string,
    eventType: string,
    data: Record<string, unknown>,
  ): void {
    const session = this.activeSessions.get(callId);
    if (!session) return;

    const event = {
      callId,
      tenantId: session.tenantId,
      type: eventType,
      data,
      timestamp: new Date().toISOString(),
    };

    // Publish to Redis for the monitoring gateway to pick up
    this.redisService.getClient().publish(
      `call:events:${session.tenantId}`,
      JSON.stringify(event),
    ).catch((err) => {
      this.logger.warn(`Failed to publish call event: ${err.message}`);
    });
  }

  /**
   * Checks compliance requirements before making a call.
   */
  private async checkCompliance(tenantId: string, contactId: string): Promise<void> {
    const contact = await this.prisma.contact.findFirst({
      where: { id: contactId, tenantId },
      select: { doNotContact: true, consentStatus: true },
    });

    if (!contact) {
      throw new Error(`Contact ${contactId} not found`);
    }

    if (contact.doNotContact) {
      throw new Error(`Contact ${contactId} is on the Do Not Contact list`);
    }

    // Check consent status if required by tenant settings
    const tenant = await this.prisma.tenant.findUnique({
      where: { id: tenantId },
      select: { settings: true },
    });

    const settings = (tenant?.settings as Record<string, unknown>) || {};
    if (settings.requireConsent && contact.consentStatus !== 'GRANTED') {
      throw new Error(`Contact ${contactId} has not granted consent (status: ${contact.consentStatus})`);
    }
  }

  /**
   * Stores session data in Redis for cross-process access.
   */
  private async storeSessionInRedis(session: CallSession): Promise<void> {
    await this.redisService.setJson(`call:session:${session.callId}`, {
      callId: session.callId,
      tenantId: session.tenantId,
      agentId: session.agentId,
      callSid: session.callSid,
      phase: session.phase,
      startedAt: session.startedAt.toISOString(),
      contactId: session.contactId,
      campaignId: session.campaignId,
    }, 7200);
  }
}
