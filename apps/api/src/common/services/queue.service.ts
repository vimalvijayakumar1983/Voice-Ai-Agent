import { Injectable, Logger } from '@nestjs/common';
import { Queue, Job, JobsOptions } from 'bullmq';
import { ConfigService } from '@nestjs/config';

export interface QueueJobData {
  tenantId: string;
  [key: string]: unknown;
}

@Injectable()
export class QueueService {
  private readonly logger = new Logger(QueueService.name);
  private readonly queues = new Map<string, Queue>();

  constructor(private readonly configService: ConfigService) {}

  getQueue(name: string): Queue {
    if (!this.queues.has(name)) {
      const queue = new Queue(name, {
        connection: {
          host: this.configService.get<string>('REDIS_HOST', 'localhost'),
          port: this.configService.get<number>('REDIS_PORT', 6379),
          password: this.configService.get<string>('REDIS_PASSWORD', '') || undefined,
        },
        defaultJobOptions: {
          attempts: 3,
          backoff: {
            type: 'exponential',
            delay: 2000,
          },
          removeOnComplete: { count: 1000 },
          removeOnFail: { count: 5000 },
        },
      });
      this.queues.set(name, queue);
    }
    return this.queues.get(name)!;
  }

  async addJob<T extends QueueJobData>(
    queueName: string,
    jobName: string,
    data: T,
    options?: JobsOptions,
  ): Promise<Job<T>> {
    const queue = this.getQueue(queueName);
    const job = await queue.add(jobName, data, options);
    this.logger.log(
      `Added job ${jobName} (${job.id}) to queue ${queueName} for tenant ${data.tenantId}`,
    );
    return job as Job<T>;
  }

  async addBulk<T extends QueueJobData>(
    queueName: string,
    jobs: Array<{ name: string; data: T; opts?: JobsOptions }>,
  ): Promise<Job<T>[]> {
    const queue = this.getQueue(queueName);
    const result = await queue.addBulk(jobs);
    this.logger.log(
      `Added ${result.length} jobs to queue ${queueName}`,
    );
    return result as Job<T>[];
  }

  async getJobCounts(queueName: string): Promise<Record<string, number>> {
    const queue = this.getQueue(queueName);
    return queue.getJobCounts();
  }

  async pauseQueue(queueName: string): Promise<void> {
    const queue = this.getQueue(queueName);
    await queue.pause();
    this.logger.log(`Paused queue ${queueName}`);
  }

  async resumeQueue(queueName: string): Promise<void> {
    const queue = this.getQueue(queueName);
    await queue.resume();
    this.logger.log(`Resumed queue ${queueName}`);
  }

  async closeAll(): Promise<void> {
    for (const [name, queue] of this.queues) {
      await queue.close();
      this.logger.log(`Closed queue ${name}`);
    }
    this.queues.clear();
  }
}
