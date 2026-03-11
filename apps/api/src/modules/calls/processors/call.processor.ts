import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { Worker, Job } from 'bullmq';
import { getRedisConnection } from '../../../common/utils/parse-redis-url';
import { PrismaService } from '../../../common/services/prisma.service';
import { RedisService } from '../../../common/services/redis.service';
import { S3Service } from '../../../common/services/s3.service';
import { QueueService } from '../../../common/services/queue.service';
import { LLMService } from '../../../services/llm/llm.service';
import { LLMMessage } from '../../../services/llm/llm.interface';
import { CallOrchestratorService } from '../call-orchestrator.service';
import { CampaignExecutorService } from '../../campaigns/campaign-executor.service';

interface InitiateCallData {
  tenantId: string;
  callId: string;
  campaignId?: string;
  contactId?: string;
  phone: string;
  agentId: string;
}

interface PostCallData {
  tenantId: string;
  callId: string;
  campaignId?: string;
  contactId?: string;
  durationSeconds: number;
  conversationHistory: LLMMessage[];
  metrics?: Record<string, unknown>;
  hasRecording: boolean;
}

type CallJobData = InitiateCallData | PostCallData;

@Injectable()
export class CallProcessor {
  private readonly logger = new Logger(CallProcessor.name);
  private worker: Worker | null = null;

  constructor(
    private readonly prisma: PrismaService,
    private readonly redisService: RedisService,
    private readonly s3Service: S3Service,
    private readonly queueService: QueueService,
    private readonly configService: ConfigService,
    private readonly llmService: LLMService,
    private readonly callOrchestrator: CallOrchestratorService,
    private readonly campaignExecutor: CampaignExecutorService,
  ) {
    this.initializeWorker();
  }

  private initializeWorker(): void {
    this.worker = new Worker(
      'call-processing',
      async (job: Job<CallJobData>) => {
        return this.processJob(job);
      },
      {
        connection: getRedisConnection(this.configService),
        concurrency: 20,
        limiter: {
          max: 100,
          duration: 1000,
        },
      },
    );

    this.worker.on('completed', (job: Job) => {
      this.logger.debug(`Call job ${job.name} (${job.id}) completed`);
    });

    this.worker.on('failed', (job: Job | undefined, error: Error) => {
      this.logger.error(`Call job ${job?.name} (${job?.id}) failed: ${error.message}`);
    });

    this.worker.on('error', (error: Error) => {
      this.logger.error(`Call worker error: ${error.message}`);
    });

    this.logger.log('Call processor worker initialized');
  }

  private async processJob(job: Job<CallJobData>): Promise<unknown> {
    this.logger.log(`Processing call job: ${job.name} (${job.id})`);

    switch (job.name) {
      case 'initiate-call':
        return this.handleInitiateCall(job.data as InitiateCallData);

      case 'post-call':
        return this.handlePostCall(job.data as PostCallData);

      default:
        this.logger.warn(`Unknown call job type: ${job.name}`);
        return null;
    }
  }

  /**
   * Initiates a call through the call orchestrator.
   */
  private async handleInitiateCall(data: InitiateCallData): Promise<void> {
    this.logger.log(`Initiating call ${data.callId} to ${data.phone}`);

    try {
      // Get a from number for the tenant
      const fromNumber = await this.getFromNumber(data.tenantId);

      await this.callOrchestrator.initiateCall({
        callId: data.callId,
        tenantId: data.tenantId,
        agentId: data.agentId,
        contactId: data.contactId,
        campaignId: data.campaignId,
        toNumber: data.phone,
        fromNumber,
      });
    } catch (error) {
      this.logger.error(`Failed to initiate call ${data.callId}: ${(error as Error).message}`);

      // Update call status to FAILED
      await this.prisma.call.update({
        where: { id: data.callId },
        data: {
          status: 'FAILED',
          metadata: {
            error: (error as Error).message,
            failedAt: new Date().toISOString(),
          },
        },
      });

      // Notify campaign executor if this is a campaign call
      if (data.campaignId && data.contactId) {
        await this.campaignExecutor.handleCallCompleted(
          data.campaignId,
          data.callId,
          {
            status: 'FAILED',
            durationSeconds: 0,
            contactId: data.contactId,
          },
        );
      }

      throw error;
    }
  }

  /**
   * Handles post-call processing after a call has ended.
   * Generates transcript, analyzes sentiment, classifies outcome,
   * uploads recording, and updates all relevant records.
   */
  private async handlePostCall(data: PostCallData): Promise<void> {
    const { callId, tenantId, durationSeconds, conversationHistory } = data;

    this.logger.log(`Post-call processing for call ${callId}`);

    try {
      // 1. Save transcript
      const transcript = await this.saveTranscript(callId, tenantId, conversationHistory);

      // 2. Analyze sentiment and generate summary
      const analysis = await this.analyzeCall(conversationHistory);

      // 3. Classify call outcome
      const outcome = this.classifyOutcome(conversationHistory, durationSeconds);

      // 4. Upload recording if available
      let recordingKey: string | undefined;
      if (data.hasRecording) {
        recordingKey = await this.uploadRecording(callId, tenantId);
      }

      // 5. Update call record with analysis results
      await this.prisma.call.update({
        where: { id: callId },
        data: {
          outcome: outcome.disposition,
          sentiment: analysis.sentiment,
          recordingStorageKey: recordingKey,
          metadata: {
            summary: analysis.summary,
            sentimentScore: analysis.sentimentScore,
            keyTopics: analysis.keyTopics,
            outcome: outcome.disposition,
            outcomeDetails: outcome.details,
            pipelineMetrics: data.metrics || {},
            recordingKey,
          } as any,
        },
      });

      // 6. Update contact record with call outcome
      if (data.contactId) {
        await this.updateContactAfterCall(data.contactId, tenantId, {
          lastCallAt: new Date(),
          lastCallOutcome: outcome.disposition,
          sentiment: analysis.sentiment,
        });
      }

      // 7. Notify campaign executor if this is a campaign call
      if (data.campaignId && data.contactId) {
        await this.campaignExecutor.handleCallCompleted(
          data.campaignId,
          callId,
          {
            status: outcome.disposition === 'connected' ? 'COMPLETED' : 'FAILED',
            durationSeconds,
            disposition: outcome.disposition,
            contactId: data.contactId,
          },
        );
      }

      // 8. Send webhooks if configured
      await this.sendWebhooks(tenantId, callId, {
        callId,
        status: 'completed',
        duration: durationSeconds,
        sentiment: analysis.sentiment,
        outcome: outcome.disposition,
        summary: analysis.summary,
      });

      this.logger.log(
        `Post-call processing complete for ${callId}: ` +
        `outcome=${outcome.disposition}, sentiment=${analysis.sentiment}`,
      );
    } catch (error) {
      this.logger.error(`Post-call processing error for ${callId}: ${(error as Error).message}`);
    }
  }

  /**
   * Saves the call transcript to the database.
   */
  private async saveTranscript(
    callId: string,
    tenantId: string,
    conversationHistory: LLMMessage[],
  ): Promise<unknown> {
    // Filter out system messages for the stored transcript
    const transcriptEntries = conversationHistory
      .filter((m) => m.role !== 'system')
      .map((m, index) => ({
        speaker: m.role === 'user' ? 'human' : 'ai',
        text: m.content,
        timestamp: new Date(Date.now() - (conversationHistory.length - index) * 5000).toISOString(),
      }));

    const transcript = await this.prisma.callTranscript.create({
      data: {
        callId,
        entries: transcriptEntries,
        fullText: transcriptEntries.map((e) => `${e.speaker}: ${e.text}`).join('\n'),
        wordCount: transcriptEntries.reduce((sum, e) => sum + e.text.split(' ').length, 0),
      },
    });

    this.logger.log(`Transcript saved for call ${callId}: ${transcriptEntries.length} entries`);
    return transcript;
  }

  /**
   * Uses LLM to analyze the call for sentiment, summary, and key topics.
   */
  private async analyzeCall(conversationHistory: LLMMessage[]): Promise<{
    sentiment: string;
    sentimentScore: number;
    summary: string;
    keyTopics: string[];
  }> {
    // Build a transcript text for analysis
    const transcriptText = conversationHistory
      .filter((m) => m.role !== 'system')
      .map((m) => `${m.role === 'user' ? 'Customer' : 'AI Agent'}: ${m.content}`)
      .join('\n');

    if (!transcriptText.trim()) {
      return {
        sentiment: 'neutral',
        sentimentScore: 0,
        summary: 'No conversation recorded.',
        keyTopics: [],
      };
    }

    try {
      const result = await this.llmService.complete([
        {
          role: 'system',
          content: `You are a call analysis assistant. Analyze the following call transcript and respond with JSON only.
The JSON must have these fields:
- sentiment: one of "positive", "neutral", "negative"
- sentimentScore: number from -1.0 to 1.0
- summary: a concise 1-2 sentence summary of the call
- keyTopics: array of up to 5 key topics discussed

Respond with ONLY valid JSON, no markdown or explanation.`,
        },
        {
          role: 'user',
          content: transcriptText,
        },
      ], {
        temperature: 0.3,
        maxTokens: 500,
      });

      try {
        const analysis = JSON.parse(result.content);
        return {
          sentiment: analysis.sentiment || 'neutral',
          sentimentScore: analysis.sentimentScore || 0,
          summary: analysis.summary || 'Call analysis unavailable.',
          keyTopics: analysis.keyTopics || [],
        };
      } catch {
        this.logger.warn('Failed to parse call analysis JSON, using defaults');
        return {
          sentiment: 'neutral',
          sentimentScore: 0,
          summary: result.content.slice(0, 200),
          keyTopics: [],
        };
      }
    } catch (error) {
      this.logger.error(`Call analysis failed: ${(error as Error).message}`);
      return {
        sentiment: 'neutral',
        sentimentScore: 0,
        summary: 'Analysis unavailable due to processing error.',
        keyTopics: [],
      };
    }
  }

  /**
   * Classifies the call outcome based on conversation content and duration.
   */
  private classifyOutcome(
    conversationHistory: LLMMessage[],
    durationSeconds: number,
  ): {
    disposition: string;
    details: string;
  } {
    const userMessages = conversationHistory.filter((m) => m.role === 'user');
    const assistantMessages = conversationHistory.filter((m) => m.role === 'assistant');

    // No conversation means no answer or very short call
    if (userMessages.length === 0) {
      if (durationSeconds < 5) {
        return { disposition: 'no_answer', details: 'Call was not answered or ended immediately' };
      }
      return { disposition: 'voicemail', details: 'No customer response detected' };
    }

    // Very short conversation
    if (durationSeconds < 15 && userMessages.length <= 1) {
      return { disposition: 'quick_hangup', details: 'Customer ended call quickly' };
    }

    // Check for transfer/handoff patterns in the conversation
    const fullText = conversationHistory.map((m) => m.content).join(' ').toLowerCase();
    if (fullText.includes('transfer') || fullText.includes('speak to a human') || fullText.includes('real person')) {
      return { disposition: 'transferred', details: 'Call transferred to human agent' };
    }

    // Check for appointment/meeting scheduling
    if (fullText.includes('appointment') || fullText.includes('schedule') || fullText.includes('meeting')) {
      return { disposition: 'appointment_set', details: 'Appointment or meeting was scheduled' };
    }

    // Check for interest/positive outcome
    if (fullText.includes('interested') || fullText.includes('sign up') || fullText.includes('yes, please')) {
      return { disposition: 'interested', details: 'Customer showed interest' };
    }

    // Check for rejection
    if (fullText.includes('not interested') || fullText.includes('no thank') || fullText.includes('remove me')) {
      return { disposition: 'not_interested', details: 'Customer declined' };
    }

    // Default: connected conversation
    if (userMessages.length >= 2 && durationSeconds >= 30) {
      return { disposition: 'connected', details: 'Meaningful conversation completed' };
    }

    return { disposition: 'connected', details: 'Call completed' };
  }

  /**
   * Uploads the call recording to S3.
   */
  private async uploadRecording(callId: string, tenantId: string): Promise<string> {
    // The recording buffer would come from the call orchestrator's session
    // For now, we construct the S3 key and handle the upload
    const key = this.s3Service.buildKey(
      tenantId,
      'recordings',
      `${new Date().toISOString().slice(0, 10)}`,
      `${callId}.wav`,
    );

    // In production, the recording chunks are assembled into a WAV file
    // and uploaded here. The chunks are stored in the CallSession.
    const recordingData = await this.redisService.get(`call:recording:${callId}`);
    if (recordingData) {
      const buffer = Buffer.from(recordingData, 'base64');
      await this.s3Service.upload(key, buffer, 'audio/wav', {
        callId,
        tenantId,
      });
      await this.redisService.del(`call:recording:${callId}`);
    }

    this.logger.log(`Recording uploaded for call ${callId}: ${key}`);
    return key;
  }

  /**
   * Updates the contact record after a call.
   */
  private async updateContactAfterCall(
    contactId: string,
    tenantId: string,
    data: {
      lastCallAt: Date;
      lastCallOutcome: string;
      sentiment: string;
    },
  ): Promise<void> {
    try {
      await this.prisma.contact.update({
        where: { id: contactId },
        data: {
          lastContactedAt: data.lastCallAt,
          metadata: {
            lastCallOutcome: data.lastCallOutcome,
            lastCallSentiment: data.sentiment,
          },
        },
      });
    } catch (error) {
      this.logger.warn(`Failed to update contact ${contactId}: ${(error as Error).message}`);
    }
  }

  /**
   * Sends webhook notifications for call completion.
   */
  private async sendWebhooks(
    tenantId: string,
    callId: string,
    payload: Record<string, unknown>,
  ): Promise<void> {
    // Look up webhook configuration for the tenant
    const webhookUrl = await this.redisService.get(`tenant:webhook:${tenantId}`);
    if (!webhookUrl) return;

    try {
      await fetch(webhookUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          event: 'call.completed',
          callId,
          tenantId,
          timestamp: new Date().toISOString(),
          ...payload,
        }),
      });
      this.logger.log(`Webhook sent for call ${callId} to ${webhookUrl}`);
    } catch (error) {
      this.logger.warn(`Webhook delivery failed for call ${callId}: ${(error as Error).message}`);
    }
  }

  /**
   * Gets a "from" phone number for a tenant.
   */
  private async getFromNumber(tenantId: string): Promise<string> {
    // Check Redis cache
    const cached = await this.redisService.get(`tenant:fromNumber:${tenantId}`);
    if (cached) return cached;

    // Look up from database
    const tenant = await this.prisma.tenant.findUnique({
      where: { id: tenantId },
      select: { settings: true },
    });

    const settings = (tenant?.settings as Record<string, unknown>) || {};
    const fromNumber = (settings.defaultFromNumber as string) ||
      this.configService.get<string>('DEFAULT_FROM_NUMBER', '+10000000000');

    await this.redisService.set(`tenant:fromNumber:${tenantId}`, fromNumber, 3600);
    return fromNumber;
  }

  /**
   * Gracefully shuts down the worker.
   */
  async shutdown(): Promise<void> {
    if (this.worker) {
      await this.worker.close();
      this.logger.log('Call processor worker shut down');
    }
  }
}
