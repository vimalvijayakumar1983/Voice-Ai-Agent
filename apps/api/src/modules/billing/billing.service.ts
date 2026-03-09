import { Injectable, NotFoundException, Logger } from '@nestjs/common';
import { PrismaService } from '../../common/services/prisma.service';
import { PaginationQuery, PaginatedResponse } from '../../common/dto/pagination.dto';

@Injectable()
export class BillingService {
  private readonly logger = new Logger(BillingService.name);

  constructor(private readonly prisma: PrismaService) {}

  async getSubscription(tenantId: string): Promise<any> {
    const subscription = await this.prisma.subscription.findFirst({
      where: { tenantId, status: 'ACTIVE' },
      include: { plan: true },
    });

    if (!subscription) {
      return {
        plan: 'FREE',
        status: 'ACTIVE',
        features: {
          maxAgents: 1,
          maxCallsPerMonth: 100,
          maxContacts: 500,
          maxUsers: 2,
        },
      };
    }

    return subscription;
  }

  async getUsage(tenantId: string): Promise<any> {
    const now = new Date();
    const startOfMonth = new Date(now.getFullYear(), now.getMonth(), 1);

    const [callCount, callMinutes, contactCount, agentCount, userCount] =
      await Promise.all([
        this.prisma.call.count({
          where: { tenantId, startedAt: { gte: startOfMonth } },
        }),
        this.prisma.call.aggregate({
          where: {
            tenantId,
            startedAt: { gte: startOfMonth },
            status: 'COMPLETED',
          },
          _sum: { durationSeconds: true },
        }),
        this.prisma.contact.count({ where: { tenantId } }),
        this.prisma.agent.count({ where: { tenantId, isActive: true } }),
        this.prisma.user.count({ where: { tenantId, isActive: true } }),
      ]);

    return {
      period: {
        start: startOfMonth.toISOString(),
        end: now.toISOString(),
      },
      usage: {
        calls: callCount,
        callMinutes: Math.round((callMinutes._sum.durationSeconds || 0) / 60),
        contacts: contactCount,
        agents: agentCount,
        users: userCount,
      },
    };
  }

  async getInvoices(tenantId: string, query: PaginationQuery): Promise<PaginatedResponse<any>> {
    const { page, limit, sortBy, sortOrder } = query;
    const skip = (page - 1) * limit;

    const where = { tenantId };

    const [invoices, total] = await Promise.all([
      this.prisma.invoice.findMany({
        where,
        skip,
        take: limit,
        orderBy: { [sortBy || 'createdAt']: sortOrder || 'desc' },
      }),
      this.prisma.invoice.count({ where }),
    ]);

    return new PaginatedResponse(invoices, total, query);
  }

  async getInvoiceById(tenantId: string, invoiceId: string): Promise<any> {
    const invoice = await this.prisma.invoice.findFirst({
      where: { id: invoiceId, tenantId },
      include: { lineItems: true },
    });

    if (!invoice) throw new NotFoundException(`Invoice ${invoiceId} not found`);
    return invoice;
  }
}
