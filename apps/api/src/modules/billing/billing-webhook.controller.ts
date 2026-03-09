import {
  Controller,
  Post,
  Req,
  Headers,
  HttpCode,
  HttpStatus,
  Logger,
  BadRequestException,
  RawBodyRequest,
} from '@nestjs/common';
import { ApiTags, ApiOperation, ApiExcludeEndpoint } from '@nestjs/swagger';
import { Request } from 'express';
import { PrismaService } from '../../common/services/prisma.service';
import { RedisService } from '../../common/services/redis.service';
import { StripeService } from './stripe.service';
import { PlansService, PlanTier } from './plans.service';
import { UsageMeteringService } from './usage-metering.service';

// ---------- Stripe Event Types ----------

interface StripeEvent {
  id: string;
  type: string;
  data: {
    object: Record<string, unknown>;
    previous_attributes?: Record<string, unknown>;
  };
  created: number;
  livemode: boolean;
}

// ---------- Controller ----------

@ApiTags('billing')
@Controller('billing/webhooks')
export class BillingWebhookController {
  private readonly logger = new Logger(BillingWebhookController.name);

  constructor(
    private readonly stripeService: StripeService,
    private readonly plansService: PlansService,
    private readonly usageMeteringService: UsageMeteringService,
    private readonly prisma: PrismaService,
    private readonly redisService: RedisService,
  ) {}

  @Post('stripe')
  @HttpCode(HttpStatus.OK)
  @ApiOperation({ summary: 'Handle Stripe webhook events' })
  @ApiExcludeEndpoint()
  async handleStripeWebhook(
    @Req() req: RawBodyRequest<Request>,
    @Headers('stripe-signature') signature: string,
  ) {
    // Verify webhook signature
    const rawBody = req.rawBody || req.body;
    if (!rawBody) {
      throw new BadRequestException('Missing request body');
    }

    const bodyString = typeof rawBody === 'string' ? rawBody : rawBody.toString();

    const isValid = this.stripeService.verifyWebhookSignature(bodyString, signature);
    if (!isValid) {
      this.logger.warn('Invalid Stripe webhook signature');
      throw new BadRequestException('Invalid webhook signature');
    }

    const event: StripeEvent = typeof rawBody === 'string'
      ? JSON.parse(rawBody)
      : JSON.parse(rawBody.toString());

    // Idempotency check
    const idempotencyKey = `stripe:webhook:${event.id}`;
    const alreadyProcessed = await this.redisService.get(idempotencyKey);
    if (alreadyProcessed) {
      this.logger.debug(`Webhook event ${event.id} already processed, skipping`);
      return { received: true, duplicate: true };
    }

    // Mark as processing (24h TTL)
    await this.redisService.set(idempotencyKey, 'processing', 86400);

    try {
      await this.processEvent(event);

      // Mark as completed
      await this.redisService.set(idempotencyKey, 'completed', 86400);

      return { received: true };
    } catch (error) {
      this.logger.error(
        `Failed to process Stripe webhook ${event.type} (${event.id}): ${(error as Error).message}`,
        (error as Error).stack,
      );

      // Mark as failed so it can be retried
      await this.redisService.del(idempotencyKey);

      throw error;
    }
  }

  // ---------- Event Routing ----------

  private async processEvent(event: StripeEvent): Promise<void> {
    this.logger.log(`Processing Stripe event: ${event.type} (${event.id})`);

    switch (event.type) {
      case 'checkout.session.completed':
        await this.handleCheckoutCompleted(event);
        break;

      case 'invoice.paid':
        await this.handleInvoicePaid(event);
        break;

      case 'invoice.payment_failed':
        await this.handleInvoicePaymentFailed(event);
        break;

      case 'customer.subscription.updated':
        await this.handleSubscriptionUpdated(event);
        break;

      case 'customer.subscription.deleted':
        await this.handleSubscriptionDeleted(event);
        break;

      case 'customer.subscription.trial_will_end':
        await this.handleTrialWillEnd(event);
        break;

      case 'invoice.upcoming':
        await this.handleUpcomingInvoice(event);
        break;

      default:
        this.logger.debug(`Unhandled Stripe event type: ${event.type}`);
    }
  }

  // ---------- Event Handlers ----------

  private async handleCheckoutCompleted(event: StripeEvent): Promise<void> {
    const session = event.data.object;
    const customerId = session.customer as string;
    const subscriptionId = session.subscription as string;

    if (!subscriptionId) return;

    // Find tenant by Stripe customer ID
    const tenantId = await this.findTenantByCustomerId(customerId);
    if (!tenantId) {
      this.logger.warn(`No tenant found for Stripe customer ${customerId}`);
      return;
    }

    // Get subscription details
    const subscription = await this.stripeService.getSubscription(subscriptionId);
    const planTier = (subscription.metadata.plan as PlanTier) || PlanTier.STARTER;

    // Update tenant with subscription info
    const tenant = await this.prisma.tenant.findUnique({ where: { id: tenantId } });
    const existingMetadata = (tenant?.metadata as Record<string, unknown>) || {};

    await this.prisma.tenant.update({
      where: { id: tenantId },
      data: {
        metadata: {
          ...existingMetadata,
          plan: planTier,
          stripeCustomerId: customerId,
          stripeSubscriptionId: subscriptionId,
          subscriptionStatus: subscription.status,
          isTrial: !!subscription.trial_end,
        } as any,
      },
    });

    // Clear plan cache
    await this.redisService.del(`tenant:plan:${tenantId}`);

    // Notify tenant via WebSocket
    await this.publishEvent(tenantId, {
      type: 'billing:checkout:completed',
      plan: planTier,
      subscriptionId,
    });

    this.logger.log(
      `Checkout completed for tenant ${tenantId}: plan=${planTier}, subscription=${subscriptionId}`,
    );
  }

  private async handleInvoicePaid(event: StripeEvent): Promise<void> {
    const invoice = event.data.object;
    const customerId = invoice.customer as string;
    const subscriptionId = invoice.subscription as string;
    const amountPaid = invoice.amount_paid as number;

    const tenantId = await this.findTenantByCustomerId(customerId);
    if (!tenantId) return;

    // Reset monthly usage counters on successful payment
    if (subscriptionId) {
      await this.usageMeteringService.resetMonthlyUsage(tenantId);
    }

    // Store invoice record
    await this.storeInvoiceEvent(tenantId, 'paid', {
      invoiceId: invoice.id,
      subscriptionId,
      amountPaid,
      currency: invoice.currency,
    });

    await this.publishEvent(tenantId, {
      type: 'billing:invoice:paid',
      invoiceId: invoice.id,
      amount: amountPaid,
    });

    this.logger.log(
      `Invoice paid for tenant ${tenantId}: $${(amountPaid / 100).toFixed(2)}`,
    );
  }

  private async handleInvoicePaymentFailed(event: StripeEvent): Promise<void> {
    const invoice = event.data.object;
    const customerId = invoice.customer as string;
    const attemptCount = invoice.attempt_count as number;

    const tenantId = await this.findTenantByCustomerId(customerId);
    if (!tenantId) return;

    // Update subscription status
    const tenant = await this.prisma.tenant.findUnique({ where: { id: tenantId } });
    const existingMetadata = (tenant?.metadata as Record<string, unknown>) || {};

    await this.prisma.tenant.update({
      where: { id: tenantId },
      data: {
        metadata: {
          ...existingMetadata,
          subscriptionStatus: 'past_due',
          paymentFailedAt: new Date().toISOString(),
          paymentAttemptCount: attemptCount,
        } as any,
      },
    });

    await this.storeInvoiceEvent(tenantId, 'payment_failed', {
      invoiceId: invoice.id,
      attemptCount,
    });

    await this.publishEvent(tenantId, {
      type: 'billing:payment:failed',
      invoiceId: invoice.id,
      attemptCount,
    });

    this.logger.warn(
      `Payment failed for tenant ${tenantId} (attempt ${attemptCount})`,
    );
  }

  private async handleSubscriptionUpdated(event: StripeEvent): Promise<void> {
    const subscription = event.data.object;
    const customerId = subscription.customer as string;
    const status = subscription.status as string;

    const tenantId = await this.findTenantByCustomerId(customerId);
    if (!tenantId) return;

    const planTier = (subscription.metadata as Record<string, string>)?.plan as PlanTier;

    const tenant = await this.prisma.tenant.findUnique({ where: { id: tenantId } });
    const existingMetadata = (tenant?.metadata as Record<string, unknown>) || {};

    const updates: Record<string, unknown> = {
      ...existingMetadata,
      subscriptionStatus: status,
      subscriptionUpdatedAt: new Date().toISOString(),
    };

    if (planTier) {
      updates.plan = planTier;
    }

    if (subscription.cancel_at_period_end) {
      updates.cancelAtPeriodEnd = true;
      updates.currentPeriodEnd = new Date(
        (subscription.current_period_end as number) * 1000,
      ).toISOString();
    } else {
      updates.cancelAtPeriodEnd = false;
    }

    await this.prisma.tenant.update({
      where: { id: tenantId },
      data: { metadata: updates as any },
    });

    await this.redisService.del(`tenant:plan:${tenantId}`);

    await this.publishEvent(tenantId, {
      type: 'billing:subscription:updated',
      status,
      plan: planTier,
      cancelAtPeriodEnd: subscription.cancel_at_period_end,
    });

    this.logger.log(
      `Subscription updated for tenant ${tenantId}: status=${status}, plan=${planTier}`,
    );
  }

  private async handleSubscriptionDeleted(event: StripeEvent): Promise<void> {
    const subscription = event.data.object;
    const customerId = subscription.customer as string;

    const tenantId = await this.findTenantByCustomerId(customerId);
    if (!tenantId) return;

    // Downgrade to free plan
    const tenant = await this.prisma.tenant.findUnique({ where: { id: tenantId } });
    const existingMetadata = (tenant?.metadata as Record<string, unknown>) || {};

    await this.prisma.tenant.update({
      where: { id: tenantId },
      data: {
        metadata: {
          ...existingMetadata,
          plan: PlanTier.FREE,
          subscriptionStatus: 'canceled',
          canceledAt: new Date().toISOString(),
          stripeSubscriptionId: null,
        } as any,
      },
    });

    await this.redisService.del(`tenant:plan:${tenantId}`);

    await this.publishEvent(tenantId, {
      type: 'billing:subscription:canceled',
      plan: PlanTier.FREE,
    });

    this.logger.log(`Subscription canceled for tenant ${tenantId}, downgraded to free`);
  }

  private async handleTrialWillEnd(event: StripeEvent): Promise<void> {
    const subscription = event.data.object;
    const customerId = subscription.customer as string;
    const trialEnd = subscription.trial_end as number;

    const tenantId = await this.findTenantByCustomerId(customerId);
    if (!tenantId) return;

    await this.publishEvent(tenantId, {
      type: 'billing:trial:ending',
      trialEndsAt: new Date(trialEnd * 1000).toISOString(),
    });

    this.logger.log(
      `Trial ending for tenant ${tenantId} at ${new Date(trialEnd * 1000).toISOString()}`,
    );
  }

  private async handleUpcomingInvoice(event: StripeEvent): Promise<void> {
    const invoice = event.data.object;
    const customerId = invoice.customer as string;

    const tenantId = await this.findTenantByCustomerId(customerId);
    if (!tenantId) return;

    // Report current usage to Stripe before invoice is finalized
    await this.usageMeteringService.reportUsageToStripe(tenantId);

    this.logger.log(`Reported usage for upcoming invoice for tenant ${tenantId}`);
  }

  // ---------- Helpers ----------

  private async findTenantByCustomerId(customerId: string): Promise<string | null> {
    // Check cache
    const cached = await this.redisService.get(`stripe:customer:tenant:${customerId}`);
    if (cached) return cached;

    // Query database
    const tenants = await this.prisma.tenant.findMany({
      where: {
        metadata: {
          path: ['stripeCustomerId'],
          equals: customerId,
        } as any,
      },
    });

    if (tenants.length === 0) return null;

    const tenantId = tenants[0].id;

    // Cache for 1 hour
    await this.redisService.set(`stripe:customer:tenant:${customerId}`, tenantId, 3600);

    return tenantId;
  }

  private async storeInvoiceEvent(
    tenantId: string,
    eventType: string,
    details: Record<string, unknown>,
  ): Promise<void> {
    try {
      await this.prisma.auditLog.create({
        data: {
          tenantId,
          action: `billing:invoice:${eventType}`,
          entityType: 'invoice',
          entityId: details.invoiceId as string || 'unknown',
          metadata: details as any,
        },
      });
    } catch (error) {
      this.logger.warn(`Failed to store invoice event: ${(error as Error).message}`);
    }
  }

  private async publishEvent(
    tenantId: string,
    event: Record<string, unknown>,
  ): Promise<void> {
    try {
      const client = this.redisService.getClient();
      await client.publish(
        'billing:events',
        JSON.stringify({ tenantId, ...event, timestamp: new Date().toISOString() }),
      );
    } catch (error) {
      this.logger.warn(`Failed to publish billing event: ${(error as Error).message}`);
    }
  }
}
