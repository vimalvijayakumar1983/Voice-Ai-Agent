import { Injectable, Logger, BadRequestException, NotFoundException } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { PrismaService } from '../../common/services/prisma.service';

// ---------- Stripe Types (mirroring Stripe SDK) ----------

interface StripeCustomer {
  id: string;
  email: string;
  name?: string;
  metadata: Record<string, string>;
  default_payment_method?: string;
}

interface StripeSubscription {
  id: string;
  customer: string;
  status: string;
  current_period_start: number;
  current_period_end: number;
  cancel_at_period_end: boolean;
  items: { data: Array<{ id: string; price: { id: string; product: string } }> };
  metadata: Record<string, string>;
  trial_end?: number;
}

interface StripeInvoice {
  id: string;
  customer: string;
  subscription: string;
  status: string;
  amount_due: number;
  amount_paid: number;
  currency: string;
  period_start: number;
  period_end: number;
  hosted_invoice_url?: string;
  pdf?: string;
  lines: { data: Array<{ description: string; amount: number; quantity: number }> };
}

interface StripePaymentMethod {
  id: string;
  type: string;
  card?: { brand: string; last4: string; exp_month: number; exp_year: number };
}

interface StripeUsageRecord {
  id: string;
  subscription_item: string;
  quantity: number;
  timestamp: number;
}

// ---------- Service ----------

@Injectable()
export class StripeService {
  private readonly logger = new Logger(StripeService.name);
  private readonly apiKey: string;
  private readonly webhookSecret: string;
  private readonly baseUrl = 'https://api.stripe.com/v1';

  constructor(
    private readonly configService: ConfigService,
    private readonly prisma: PrismaService,
  ) {
    this.apiKey = this.configService.get<string>('STRIPE_SECRET_KEY', '');
    this.webhookSecret = this.configService.get<string>('STRIPE_WEBHOOK_SECRET', '');
  }

  // ---------- Customer Management ----------

  async createCustomer(
    tenantId: string,
    email: string,
    name?: string,
    metadata?: Record<string, string>,
  ): Promise<StripeCustomer> {
    const params: Record<string, string> = {
      email,
      'metadata[tenantId]': tenantId,
    };
    if (name) params.name = name;
    if (metadata) {
      for (const [key, value] of Object.entries(metadata)) {
        params[`metadata[${key}]`] = value;
      }
    }

    const customer = await this.stripeRequest<StripeCustomer>('POST', '/customers', params);

    // Store Stripe customer ID
    await this.prisma.tenant.update({
      where: { id: tenantId },
      data: {
        metadata: {
          stripeCustomerId: customer.id,
        } as any,
      },
    });

    this.logger.log(`Created Stripe customer ${customer.id} for tenant ${tenantId}`);

    return customer;
  }

  async getCustomer(customerId: string): Promise<StripeCustomer> {
    return this.stripeRequest<StripeCustomer>('GET', `/customers/${customerId}`);
  }

  async updateCustomer(
    customerId: string,
    updates: { email?: string; name?: string; metadata?: Record<string, string> },
  ): Promise<StripeCustomer> {
    const params: Record<string, string> = {};
    if (updates.email) params.email = updates.email;
    if (updates.name) params.name = updates.name;
    if (updates.metadata) {
      for (const [key, value] of Object.entries(updates.metadata)) {
        params[`metadata[${key}]`] = value;
      }
    }

    return this.stripeRequest<StripeCustomer>('POST', `/customers/${customerId}`, params);
  }

  async getOrCreateCustomer(tenantId: string, email: string, name?: string): Promise<string> {
    const tenant = await this.prisma.tenant.findUnique({ where: { id: tenantId } });
    const metadata = (tenant?.metadata as Record<string, unknown>) || {};

    if (metadata.stripeCustomerId) {
      return metadata.stripeCustomerId as string;
    }

    const customer = await this.createCustomer(tenantId, email, name);
    return customer.id;
  }

  // ---------- Subscription Management ----------

  async createSubscription(
    customerId: string,
    priceId: string,
    options?: {
      trialDays?: number;
      metadata?: Record<string, string>;
      paymentMethodId?: string;
    },
  ): Promise<StripeSubscription> {
    const params: Record<string, string> = {
      customer: customerId,
      'items[0][price]': priceId,
      payment_behavior: 'default_incomplete',
      'expand[0]': 'latest_invoice.payment_intent',
    };

    if (options?.trialDays) {
      params.trial_period_days = String(options.trialDays);
    }

    if (options?.paymentMethodId) {
      params.default_payment_method = options.paymentMethodId;
    }

    if (options?.metadata) {
      for (const [key, value] of Object.entries(options.metadata)) {
        params[`metadata[${key}]`] = value;
      }
    }

    const subscription = await this.stripeRequest<StripeSubscription>(
      'POST',
      '/subscriptions',
      params,
    );

    this.logger.log(
      `Created subscription ${subscription.id} for customer ${customerId}`,
    );

    return subscription;
  }

  async getSubscription(subscriptionId: string): Promise<StripeSubscription> {
    return this.stripeRequest<StripeSubscription>(
      'GET',
      `/subscriptions/${subscriptionId}`,
    );
  }

  async updateSubscription(
    subscriptionId: string,
    updates: {
      priceId?: string;
      cancelAtPeriodEnd?: boolean;
      metadata?: Record<string, string>;
      prorationBehavior?: 'create_prorations' | 'none' | 'always_invoice';
    },
  ): Promise<StripeSubscription> {
    const params: Record<string, string> = {};

    if (updates.priceId) {
      // Get current subscription to find item ID
      const current = await this.getSubscription(subscriptionId);
      const itemId = current.items.data[0]?.id;
      if (itemId) {
        params[`items[0][id]`] = itemId;
        params[`items[0][price]`] = updates.priceId;
      }
    }

    if (updates.cancelAtPeriodEnd !== undefined) {
      params.cancel_at_period_end = String(updates.cancelAtPeriodEnd);
    }

    if (updates.prorationBehavior) {
      params.proration_behavior = updates.prorationBehavior;
    }

    if (updates.metadata) {
      for (const [key, value] of Object.entries(updates.metadata)) {
        params[`metadata[${key}]`] = value;
      }
    }

    return this.stripeRequest<StripeSubscription>(
      'POST',
      `/subscriptions/${subscriptionId}`,
      params,
    );
  }

  async cancelSubscription(
    subscriptionId: string,
    immediately = false,
  ): Promise<StripeSubscription> {
    if (immediately) {
      return this.stripeRequest<StripeSubscription>(
        'DELETE',
        `/subscriptions/${subscriptionId}`,
      );
    }

    return this.updateSubscription(subscriptionId, { cancelAtPeriodEnd: true });
  }

  async reactivateSubscription(subscriptionId: string): Promise<StripeSubscription> {
    return this.updateSubscription(subscriptionId, { cancelAtPeriodEnd: false });
  }

  // ---------- Usage-Based Billing ----------

  async reportUsage(
    subscriptionItemId: string,
    quantity: number,
    timestamp?: number,
    action: 'increment' | 'set' = 'increment',
  ): Promise<StripeUsageRecord> {
    const params: Record<string, string> = {
      quantity: String(quantity),
      action,
    };

    if (timestamp) {
      params.timestamp = String(timestamp);
    }

    return this.stripeRequest<StripeUsageRecord>(
      'POST',
      `/subscription_items/${subscriptionItemId}/usage_records`,
      params,
    );
  }

  async getUsageSummary(
    subscriptionItemId: string,
  ): Promise<{ total_usage: number; period: { start: number; end: number } }[]> {
    const result = await this.stripeRequest<{
      data: Array<{ total_usage: number; period: { start: number; end: number } }>;
    }>('GET', `/subscription_items/${subscriptionItemId}/usage_record_summaries`);

    return result.data;
  }

  // ---------- Payment Methods ----------

  async getPaymentMethods(customerId: string): Promise<StripePaymentMethod[]> {
    const result = await this.stripeRequest<{ data: StripePaymentMethod[] }>(
      'GET',
      `/payment_methods?customer=${customerId}&type=card`,
    );
    return result.data;
  }

  async attachPaymentMethod(
    customerId: string,
    paymentMethodId: string,
  ): Promise<StripePaymentMethod> {
    const result = await this.stripeRequest<StripePaymentMethod>(
      'POST',
      `/payment_methods/${paymentMethodId}/attach`,
      { customer: customerId },
    );

    // Set as default
    await this.updateCustomer(customerId, {
      metadata: {},
    });

    await this.stripeRequest('POST', `/customers/${customerId}`, {
      'invoice_settings[default_payment_method]': paymentMethodId,
    });

    return result;
  }

  async detachPaymentMethod(paymentMethodId: string): Promise<void> {
    await this.stripeRequest('POST', `/payment_methods/${paymentMethodId}/detach`);
  }

  // ---------- Invoices ----------

  async getInvoices(
    customerId: string,
    limit = 10,
  ): Promise<StripeInvoice[]> {
    const result = await this.stripeRequest<{ data: StripeInvoice[] }>(
      'GET',
      `/invoices?customer=${customerId}&limit=${limit}`,
    );
    return result.data;
  }

  async getInvoice(invoiceId: string): Promise<StripeInvoice> {
    return this.stripeRequest<StripeInvoice>('GET', `/invoices/${invoiceId}`);
  }

  async getUpcomingInvoice(customerId: string): Promise<StripeInvoice | null> {
    try {
      return await this.stripeRequest<StripeInvoice>(
        'GET',
        `/invoices/upcoming?customer=${customerId}`,
      );
    } catch {
      return null;
    }
  }

  // ---------- Checkout Sessions ----------

  async createCheckoutSession(
    customerId: string,
    priceId: string,
    options?: {
      successUrl: string;
      cancelUrl: string;
      trialDays?: number;
      metadata?: Record<string, string>;
    },
  ): Promise<{ url: string; sessionId: string }> {
    const params: Record<string, string> = {
      customer: customerId,
      'line_items[0][price]': priceId,
      'line_items[0][quantity]': '1',
      mode: 'subscription',
      success_url: options?.successUrl || `${this.configService.get('FRONTEND_URL')}/settings/billing?session_id={CHECKOUT_SESSION_ID}`,
      cancel_url: options?.cancelUrl || `${this.configService.get('FRONTEND_URL')}/settings/billing?canceled=true`,
    };

    if (options?.trialDays) {
      params['subscription_data[trial_period_days]'] = String(options.trialDays);
    }

    if (options?.metadata) {
      for (const [key, value] of Object.entries(options.metadata)) {
        params[`subscription_data[metadata][${key}]`] = value;
      }
    }

    const session = await this.stripeRequest<{ id: string; url: string }>(
      'POST',
      '/checkout/sessions',
      params,
    );

    return { url: session.url, sessionId: session.id };
  }

  // ---------- Customer Portal ----------

  async createPortalSession(
    customerId: string,
    returnUrl?: string,
  ): Promise<{ url: string }> {
    const params: Record<string, string> = {
      customer: customerId,
      return_url: returnUrl || `${this.configService.get('FRONTEND_URL')}/settings/billing`,
    };

    const session = await this.stripeRequest<{ url: string }>(
      'POST',
      '/billing_portal/sessions',
      params,
    );

    return { url: session.url };
  }

  // ---------- Webhook Verification ----------

  verifyWebhookSignature(
    payload: string | Buffer,
    signature: string,
  ): boolean {
    if (!this.webhookSecret) {
      this.logger.warn('Stripe webhook secret not configured');
      return false;
    }

    try {
      // Stripe webhook signature verification
      const crypto = require('crypto');
      const elements = signature.split(',');
      const timestampElement = elements.find((e: string) => e.startsWith('t='));
      const signatureElement = elements.find((e: string) => e.startsWith('v1='));

      if (!timestampElement || !signatureElement) return false;

      const timestamp = timestampElement.split('=')[1];
      const expectedSignature = signatureElement.split('=')[1];

      const payloadString = typeof payload === 'string' ? payload : payload.toString();
      const signedPayload = `${timestamp}.${payloadString}`;

      const computedSignature = crypto
        .createHmac('sha256', this.webhookSecret)
        .update(signedPayload)
        .digest('hex');

      // Timing-safe comparison
      const a = Buffer.from(expectedSignature);
      const b = Buffer.from(computedSignature);
      if (a.length !== b.length) return false;

      return crypto.timingSafeEqual(a, b);
    } catch (error) {
      this.logger.error(`Webhook signature verification failed: ${(error as Error).message}`);
      return false;
    }
  }

  // ---------- HTTP Helper ----------

  private async stripeRequest<T>(
    method: string,
    path: string,
    params?: Record<string, string>,
  ): Promise<T> {
    const url = `${this.baseUrl}${path}`;
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.apiKey}`,
      'Content-Type': 'application/x-www-form-urlencoded',
    };

    const options: RequestInit = { method, headers };

    if (params && (method === 'POST' || method === 'PUT' || method === 'PATCH')) {
      options.body = new URLSearchParams(params).toString();
    } else if (params && method === 'GET') {
      const queryString = new URLSearchParams(params).toString();
      const separator = path.includes('?') ? '&' : '?';
      const fullUrl = `${this.baseUrl}${path}${separator}${queryString}`;
      const response = await fetch(fullUrl, { method, headers });
      return this.handleStripeResponse<T>(response);
    }

    const response = await fetch(url, options);
    return this.handleStripeResponse<T>(response);
  }

  private async handleStripeResponse<T>(response: Response): Promise<T> {
    const body = await response.text();

    if (!response.ok) {
      let errorMessage = `Stripe API error: ${response.status}`;
      try {
        const errorData = JSON.parse(body);
        errorMessage = errorData.error?.message || errorMessage;
      } catch {}

      this.logger.error(errorMessage);

      if (response.status === 404) {
        throw new NotFoundException(errorMessage);
      }
      throw new BadRequestException(errorMessage);
    }

    return body ? JSON.parse(body) : (undefined as unknown as T);
  }
}
