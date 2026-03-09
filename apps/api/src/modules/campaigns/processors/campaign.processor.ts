import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { Worker, Job } from 'bullmq';
import { PrismaService } from '../../../common/services/prisma.service';
import { RedisService } from '../../../common/services/redis.service';
import { QueueService } from '../../../common/services/queue.service';
import { CampaignExecutorService } from '../campaign-executor.service';
import { CallStatus } from '../../calls/dto/call.dto';

interface StartCampaignData {
  tenantId: string;
  campaignId: string;
}

interface PauseCampaignData {
  tenantId: string;
  campaignId: string;
}

interface ResumeCampaignData {
  tenantId: string;
  campaignId: string;
}

interface ProcessContactData {
  tenantId: string;
  campaignId: string;
  contactId: string;
  phone: string;
  agentId: string;
  retryCount: number;
}

interface RetryContactData {
  tenantId: string;
  campaignId: string;
  contactId: string;
  agentId: string;
  retryCount: number;
  previousStatus: string;
}

type CampaignJobData =
  | StartCampaignData
  | PauseCampaignData
  | ResumeCampaignData
  | ProcessContactData
  | RetryContactData;

@Injectable()
export class CampaignProcessor {
  private readonly logger = new Logger(CampaignProcessor.name);
  private worker: Worker | null = null;

  constructor(
    private readonly prisma: PrismaService,
    private readonly redisService: RedisService,
    private readonly queueService: QueueService,
    private readonly configService: ConfigService,
    private readonly campaignExecutor: CampaignExecutorService,
  ) {
    this.initializeWorker();
  }

  private initializeWorker(): void {
    const redisHost = this.configService.get<string>('REDIS_HOST', 'localhost');
    const redisPort = this.configService.get<number>('REDIS_PORT', 6379);
    const redisPassword = this.configService.get<string>('REDIS_PASSWORD', '') || undefined;

    this.worker = new Worker(
      'campaign-execution',
      async (job: Job<CampaignJobData>) => {
        return this.processJob(job);
      },
      {
        connection: {
          host: redisHost,
          port: redisPort,
          password: redisPassword,
        },
        concurrency: 10,
        limiter: {
          max: 50,
          duration: 1000,
        },
      },
    );

    this.worker.on('completed', (job: Job) => {
      this.logger.debug(`Campaign job ${job.name} (${job.id}) completed`);
    });

    this.worker.on('failed', (job: Job | undefined, error: Error) => {
      this.logger.error(
        `Campaign job ${job?.name} (${job?.id}) failed: ${error.message}`,
      );
    });

    this.worker.on('error', (error: Error) => {
      this.logger.error(`Campaign worker error: ${error.message}`);
    });

    this.logger.log('Campaign processor worker initialized');
  }

  private async processJob(job: Job<CampaignJobData>): Promise<unknown> {
    this.logger.log(`Processing campaign job: ${job.name} (${job.id})`);

    switch (job.name) {
      case 'start-campaign':
        return this.handleStartCampaign(job.data as StartCampaignData);

      case 'pause-campaign':
        return this.handlePauseCampaign(job.data as PauseCampaignData);

      case 'resume-campaign':
        return this.handleResumeCampaign(job.data as ResumeCampaignData);

      case 'process-contact':
        return this.handleProcessContact(job.data as ProcessContactData);

      case 'retry-contact':
        return this.handleRetryContact(job.data as RetryContactData);

      default:
        this.logger.warn(`Unknown campaign job type: ${job.name}`);
        return null;
    }
  }

  /**
   * Handles the start-campaign job. Initializes the campaign executor.
   */
  private async handleStartCampaign(data: StartCampaignData): Promise<void> {
    this.logger.log(`Starting campaign ${data.campaignId} for tenant ${data.tenantId}`);

    try {
      await this.campaignExecutor.startCampaign(data.campaignId, data.tenantId);
    } catch (error) {
      this.logger.error(`Failed to start campaign ${data.campaignId}: ${(error as Error).message}`);

      // Mark campaign as failed if it cannot start
      await this.prisma.campaign.update({
        where: { id: data.campaignId },
        data: {
          status: 'PAUSED',
          metadata: {
            lastError: (error as Error).message,
            lastErrorAt: new Date().toISOString(),
          },
        },
      });

      throw error;
    }
  }

  /**
   * Handles the pause-campaign job.
   */
  private async handlePauseCampaign(data: PauseCampaignData): Promise<void> {
    this.logger.log(`Pausing campaign ${data.campaignId}`);
    await this.campaignExecutor.pauseCampaign(data.campaignId);
  }

  /**
   * Handles the resume-campaign job.
   */
  private async handleResumeCampaign(data: ResumeCampaignData): Promise<void> {
    this.logger.log(`Resuming campaign ${data.campaignId}`);
    await this.campaignExecutor.resumeCampaign(data.campaignId, data.tenantId);
  }

  /**
   * Handles initiating a call to a specific contact within a campaign.
   */
  private async handleProcessContact(data: ProcessContactData): Promise<void> {
    const { tenantId, campaignId, contactId, phone, agentId, retryCount } = data;

    this.logger.log(
      `Processing contact ${contactId} for campaign ${campaignId}, phone: ${phone}, retry: ${retryCount}`,
    );

    try {
      // Get agent configuration
      const agent = await this.prisma.agent.findFirst({
        where: { id: agentId, tenantId, isActive: true },
      });

      if (!agent) {
        this.logger.error(`Agent ${agentId} not found or inactive`);
        await this.handleContactCallResult(campaignId, contactId, 'FAILED', 0);
        return;
      }

      // Get contact details
      const contact = await this.prisma.contact.findFirst({
        where: { id: contactId, tenantId },
      });

      if (!contact) {
        this.logger.error(`Contact ${contactId} not found`);
        return;
      }

      // Check DNC (Do Not Contact) list
      if (contact.doNotContact) {
        this.logger.log(`Contact ${contactId} is on DNC list, skipping`);
        return;
      }

      // Create the call record
      const call = await this.prisma.call.create({
        data: {
          tenantId,
          agentId,
          contactId,
          campaignId,
          direction: 'OUTBOUND',
          status: CallStatus.QUEUED,
          toNumber: phone,
          fromNumber: null, // Will be assigned by telephony provider
          metadata: {
            retryCount,
            campaignId,
            contactName: `${contact.firstName || ''} ${contact.lastName || ''}`.trim(),
          },
        },
      });

      this.logger.log(`Call ${call.id} created for contact ${contactId} in campaign ${campaignId}`);

      // Queue the actual call initiation through the call processor
      await this.queueService.addJob('call-processing', 'initiate-call', {
        tenantId,
        callId: call.id,
        campaignId,
        contactId,
        phone,
        agentId,
      });

    } catch (error) {
      this.logger.error(
        `Error processing contact ${contactId} in campaign ${campaignId}: ${(error as Error).message}`,
      );
      await this.handleContactCallResult(campaignId, contactId, 'FAILED', 0);
      throw error;
    }
  }

  /**
   * Handles retrying a failed call to a contact.
   */
  private async handleRetryContact(data: RetryContactData): Promise<void> {
    const { tenantId, campaignId, contactId, agentId, retryCount, previousStatus } = data;

    this.logger.log(
      `Retrying contact ${contactId} in campaign ${campaignId}, ` +
      `attempt ${retryCount}, previous status: ${previousStatus}`,
    );

    // Get the contact's phone number
    const contact = await this.prisma.contact.findFirst({
      where: { id: contactId, tenantId },
      select: { phone: true, doNotContact: true },
    });

    if (!contact?.phone || contact.doNotContact) {
      this.logger.log(`Contact ${contactId} no longer callable for retry`);
      return;
    }

    // Process as a regular contact call
    await this.handleProcessContact({
      tenantId,
      campaignId,
      contactId,
      phone: contact.phone,
      agentId,
      retryCount,
    });
  }

  /**
   * Reports a call result back to the campaign executor for metrics and retry handling.
   */
  private async handleContactCallResult(
    campaignId: string,
    contactId: string,
    status: string,
    durationSeconds: number,
  ): Promise<void> {
    try {
      await this.campaignExecutor.handleCallCompleted(
        campaignId,
        `${campaignId}-${contactId}`,
        {
          status,
          durationSeconds,
          contactId,
        },
      );
    } catch (error) {
      this.logger.error(
        `Failed to report call result for campaign ${campaignId}: ${(error as Error).message}`,
      );
    }
  }

  /**
   * Gracefully shuts down the worker.
   */
  async shutdown(): Promise<void> {
    if (this.worker) {
      await this.worker.close();
      this.logger.log('Campaign processor worker shut down');
    }
  }
}
