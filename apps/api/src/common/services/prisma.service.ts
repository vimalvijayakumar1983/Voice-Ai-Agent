import { Injectable, OnModuleInit, OnModuleDestroy, Logger } from '@nestjs/common';
import { PrismaClient, Prisma } from '@prisma/client';

@Injectable()
export class PrismaService
  extends PrismaClient
  implements OnModuleInit, OnModuleDestroy
{
  private readonly logger = new Logger(PrismaService.name);

  constructor() {
    super({
      log: [
        { emit: 'event', level: 'query' },
        { emit: 'event', level: 'error' },
        { emit: 'event', level: 'warn' },
      ],
    });
  }

  async onModuleInit(): Promise<void> {
    await this.$connect();
    this.logger.log('Prisma connected to database');

    (this as any).$on('query', (event: Prisma.QueryEvent) => {
      if (event.duration > 500) {
        this.logger.warn(
          `Slow query (${event.duration}ms): ${event.query}`,
        );
      }
    });

    (this as any).$on('error', (event: Prisma.LogEvent) => {
      this.logger.error(`Prisma error: ${event.message}`);
    });
  }

  async onModuleDestroy(): Promise<void> {
    await this.$disconnect();
    this.logger.log('Prisma disconnected from database');
  }

  /**
   * Helper for tenant-scoped queries. Adds tenantId filter automatically.
   */
  tenantScope(tenantId: string): { where: { tenantId: string } } {
    return { where: { tenantId } };
  }

  /**
   * Build a tenant-scoped where clause by merging with additional filters.
   */
  tenantWhere<T extends Record<string, unknown>>(
    tenantId: string,
    additionalWhere?: T,
  ): { tenantId: string } & T {
    return {
      tenantId,
      ...(additionalWhere || ({} as T)),
    };
  }

  /**
   * Execute operations within a transaction.
   */
  async executeInTransaction<T>(
    fn: (tx: Prisma.TransactionClient) => Promise<T>,
  ): Promise<T> {
    return this.$transaction(fn, {
      maxWait: 5000,
      timeout: 30000,
    });
  }
}
