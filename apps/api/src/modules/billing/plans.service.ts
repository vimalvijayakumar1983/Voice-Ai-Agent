import { Injectable, Logger, BadRequestException } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { PrismaService } from '../../common/services/prisma.service';
import { RedisService } from '../../common/services/redis.service';
import { StripeService } from './stripe.service';
import { UsageDimension } from './usage-metering.service';

// ---------- Types ----------

export enum PlanTier {
  FREE = 'free',
  STARTER = 'starter',
  PROFESSIONAL = 'professional',
  ENTERPRISE = 'enterprise',
}

export interface PlanDefinition {
  tier: PlanTier;
  name: string;
  description: string;
  monthlyPrice: number;
  annualPrice: number;
  stripePriceIdMonthly?: string;
  stripePriceIdAnnual?: string;
  features: PlanFeature[];
  limits: Record<string, number>;
  trialDays: number;
  highlighted?: boolean;
}

export interface PlanFeature {
  name: string;
  included: boolean;
  limit?: string;
  tooltip?: string;
}

export interface PlanComparison {
  feature: string;
  free: string | boolean;
  starter: string | boolean;
  professional: string | boolean;
  enterprise: string | boolean;
}

export interface PlanChangeResult {
  previousPlan: PlanTier;
  newPlan: PlanTier;
  isUpgrade: boolean;
  effectiveDate: string;
  prorationAmount?: number;
  subscriptionId?: string;
}

// ---------- Plan Definitions ----------

const PLAN_DEFINITIONS: Record<PlanTier, PlanDefinition> = {
  [PlanTier.FREE]: {
    tier: PlanTier.FREE,
    name: 'Free',
    description: 'Get started with basic voice AI capabilities',
    monthlyPrice: 0,
    annualPrice: 0,
    trialDays: 0,
    features: [
      { name: '50 call minutes/month', included: true, limit: '50' },
      { name: '1 AI agent', included: true, limit: '1' },
      { name: 'Basic analytics', included: true },
      { name: 'Email support', included: true },
      { name: 'Knowledge base (1 doc)', included: true, limit: '1' },
      { name: 'CRM integration', included: false },
      { name: 'Custom workflows', included: false },
      { name: 'API access', included: false },
      { name: 'Webhook notifications', included: false },
      { name: 'Priority support', included: false },
    ],
    limits: {
      [UsageDimension.CALL_MINUTES]: 50,
      [UsageDimension.API_CALLS]: 100,
      [UsageDimension.TRANSCRIPTION_MINUTES]: 50,
      [UsageDimension.TTS_CHARACTERS]: 50000,
      [UsageDimension.LLM_TOKENS]: 100000,
      [UsageDimension.STORAGE_GB]: 0.5,
      agents: 1,
      campaigns: 1,
      contacts: 100,
      knowledge_bases: 1,
      documents_per_kb: 1,
    },
  },
  [PlanTier.STARTER]: {
    tier: PlanTier.STARTER,
    name: 'Starter',
    description: 'Perfect for small teams getting started with voice AI',
    monthlyPrice: 49,
    annualPrice: 470,
    trialDays: 14,
    features: [
      { name: '500 call minutes/month', included: true, limit: '500' },
      { name: '5 AI agents', included: true, limit: '5' },
      { name: 'Advanced analytics', included: true },
      { name: 'Email + chat support', included: true },
      { name: 'Knowledge base (10 docs)', included: true, limit: '10' },
      { name: 'CRM integration (1)', included: true, limit: '1' },
      { name: 'Basic workflows', included: true },
      { name: 'API access', included: true },
      { name: 'Webhook notifications', included: false },
      { name: 'Priority support', included: false },
    ],
    limits: {
      [UsageDimension.CALL_MINUTES]: 500,
      [UsageDimension.API_CALLS]: 5000,
      [UsageDimension.TRANSCRIPTION_MINUTES]: 500,
      [UsageDimension.TTS_CHARACTERS]: 500000,
      [UsageDimension.LLM_TOKENS]: 1000000,
      [UsageDimension.STORAGE_GB]: 5,
      agents: 5,
      campaigns: 10,
      contacts: 1000,
      knowledge_bases: 3,
      documents_per_kb: 10,
    },
  },
  [PlanTier.PROFESSIONAL]: {
    tier: PlanTier.PROFESSIONAL,
    name: 'Professional',
    description: 'For growing businesses with advanced needs',
    monthlyPrice: 199,
    annualPrice: 1910,
    trialDays: 14,
    highlighted: true,
    features: [
      { name: '5,000 call minutes/month', included: true, limit: '5,000' },
      { name: '25 AI agents', included: true, limit: '25' },
      { name: 'Advanced analytics + reporting', included: true },
      { name: 'Priority support', included: true },
      { name: 'Knowledge base (100 docs)', included: true, limit: '100' },
      { name: 'CRM integrations (unlimited)', included: true },
      { name: 'Advanced workflows', included: true },
      { name: 'Full API access', included: true },
      { name: 'Webhook notifications', included: true },
      { name: 'Custom voice cloning', included: true },
    ],
    limits: {
      [UsageDimension.CALL_MINUTES]: 5000,
      [UsageDimension.API_CALLS]: 50000,
      [UsageDimension.TRANSCRIPTION_MINUTES]: 5000,
      [UsageDimension.TTS_CHARACTERS]: 5000000,
      [UsageDimension.LLM_TOKENS]: 10000000,
      [UsageDimension.STORAGE_GB]: 50,
      agents: 25,
      campaigns: 100,
      contacts: 25000,
      knowledge_bases: 20,
      documents_per_kb: 100,
    },
  },
  [PlanTier.ENTERPRISE]: {
    tier: PlanTier.ENTERPRISE,
    name: 'Enterprise',
    description: 'Custom solutions for large organizations',
    monthlyPrice: 799,
    annualPrice: 7670,
    trialDays: 30,
    features: [
      { name: 'Unlimited call minutes', included: true, limit: 'Unlimited' },
      { name: 'Unlimited AI agents', included: true, limit: 'Unlimited' },
      { name: 'Custom analytics + BI export', included: true },
      { name: 'Dedicated support + SLA', included: true },
      { name: 'Knowledge base (unlimited)', included: true, limit: 'Unlimited' },
      { name: 'All CRM integrations', included: true },
      { name: 'Custom workflow builder', included: true },
      { name: 'Full API + SDK access', included: true },
      { name: 'Webhook + real-time events', included: true },
      { name: 'Custom voice cloning + SSO', included: true },
    ],
    limits: {
      [UsageDimension.CALL_MINUTES]: -1, // unlimited
      [UsageDimension.API_CALLS]: -1,
      [UsageDimension.TRANSCRIPTION_MINUTES]: -1,
      [UsageDimension.TTS_CHARACTERS]: -1,
      [UsageDimension.LLM_TOKENS]: -1,
      [UsageDimension.STORAGE_GB]: 500,
      agents: -1,
      campaigns: -1,
      contacts: -1,
      knowledge_bases: -1,
      documents_per_kb: -1,
    },
  },
};

const PLAN_ORDER: PlanTier[] = [
  PlanTier.FREE,
  PlanTier.STARTER,
  PlanTier.PROFESSIONAL,
  PlanTier.ENTERPRISE,
];

// ---------- Service ----------

@Injectable()
export class PlansService {
  private readonly logger = new Logger(PlansService.name);

  constructor(
    private readonly prisma: PrismaService,
    private readonly redisService: RedisService,
    private readonly stripeService: StripeService,
    private readonly configService: ConfigService,
  ) {
    // Set Stripe price IDs from config
    PLAN_DEFINITIONS[PlanTier.STARTER].stripePriceIdMonthly =
      this.configService.get('STRIPE_PRICE_STARTER_MONTHLY', '');
    PLAN_DEFINITIONS[PlanTier.STARTER].stripePriceIdAnnual =
      this.configService.get('STRIPE_PRICE_STARTER_ANNUAL', '');
    PLAN_DEFINITIONS[PlanTier.PROFESSIONAL].stripePriceIdMonthly =
      this.configService.get('STRIPE_PRICE_PRO_MONTHLY', '');
    PLAN_DEFINITIONS[PlanTier.PROFESSIONAL].stripePriceIdAnnual =
      this.configService.get('STRIPE_PRICE_PRO_ANNUAL', '');
    PLAN_DEFINITIONS[PlanTier.ENTERPRISE].stripePriceIdMonthly =
      this.configService.get('STRIPE_PRICE_ENTERPRISE_MONTHLY', '');
    PLAN_DEFINITIONS[PlanTier.ENTERPRISE].stripePriceIdAnnual =
      this.configService.get('STRIPE_PRICE_ENTERPRISE_ANNUAL', '');
  }

  // ---------- Plan Queries ----------

  getAllPlans(): PlanDefinition[] {
    return Object.values(PLAN_DEFINITIONS);
  }

  getPlan(tier: PlanTier): PlanDefinition {
    const plan = PLAN_DEFINITIONS[tier];
    if (!plan) {
      throw new BadRequestException(`Unknown plan tier: ${tier}`);
    }
    return plan;
  }

  getPlanLimits(tier: PlanTier): Record<string, number> {
    return { ...PLAN_DEFINITIONS[tier].limits };
  }

  getFeatureMatrix(): PlanComparison[] {
    const allFeatureNames = new Set<string>();
    for (const plan of Object.values(PLAN_DEFINITIONS)) {
      for (const feature of plan.features) {
        allFeatureNames.add(feature.name);
      }
    }

    const matrix: PlanComparison[] = [];

    for (const featureName of allFeatureNames) {
      const row: PlanComparison = {
        feature: featureName,
        free: false,
        starter: false,
        professional: false,
        enterprise: false,
      };

      for (const tier of PLAN_ORDER) {
        const plan = PLAN_DEFINITIONS[tier];
        const feature = plan.features.find((f) => f.name === featureName);
        if (feature) {
          (row as any)[tier] = feature.included
            ? feature.limit || true
            : false;
        }
      }

      matrix.push(row);
    }

    return matrix;
  }

  // ---------- Tenant Plan Management ----------

  async getTenantPlan(tenantId: string): Promise<PlanTier> {
    // Check cache first
    const cached = await this.redisService.get(`tenant:plan:${tenantId}`);
    if (cached) return cached as PlanTier;

    const tenant = await this.prisma.tenant.findUnique({
      where: { id: tenantId },
    });

    const metadata = (tenant?.metadata as Record<string, unknown>) || {};
    const plan = (metadata.plan as PlanTier) || PlanTier.FREE;

    // Cache for 5 minutes
    await this.redisService.set(`tenant:plan:${tenantId}`, plan, 300);

    return plan;
  }

  async changePlan(
    tenantId: string,
    newTier: PlanTier,
    billingCycle: 'monthly' | 'annual' = 'monthly',
    email?: string,
  ): Promise<PlanChangeResult> {
    const currentTier = await this.getTenantPlan(tenantId);

    if (currentTier === newTier) {
      throw new BadRequestException('Already on this plan');
    }

    const currentIndex = PLAN_ORDER.indexOf(currentTier);
    const newIndex = PLAN_ORDER.indexOf(newTier);
    const isUpgrade = newIndex > currentIndex;

    const newPlan = this.getPlan(newTier);
    let subscriptionId: string | undefined;
    let prorationAmount: number | undefined;

    // Handle Stripe subscription changes
    if (newTier === PlanTier.FREE) {
      // Downgrade to free: cancel subscription
      const tenant = await this.prisma.tenant.findUnique({ where: { id: tenantId } });
      const metadata = (tenant?.metadata as Record<string, unknown>) || {};
      const existingSubId = metadata.stripeSubscriptionId as string;

      if (existingSubId) {
        await this.stripeService.cancelSubscription(existingSubId, false);
      }
    } else {
      // Create or update subscription
      const priceId = billingCycle === 'annual'
        ? newPlan.stripePriceIdAnnual
        : newPlan.stripePriceIdMonthly;

      if (!priceId) {
        throw new BadRequestException(`No Stripe price configured for ${newTier} ${billingCycle}`);
      }

      const customerId = await this.stripeService.getOrCreateCustomer(
        tenantId,
        email || '',
      );

      const tenant = await this.prisma.tenant.findUnique({ where: { id: tenantId } });
      const metadata = (tenant?.metadata as Record<string, unknown>) || {};
      const existingSubId = metadata.stripeSubscriptionId as string;

      if (existingSubId && currentTier !== PlanTier.FREE) {
        // Update existing subscription (proration)
        const updated = await this.stripeService.updateSubscription(existingSubId, {
          priceId,
          prorationBehavior: 'create_prorations',
          metadata: { tenantId, plan: newTier },
        });
        subscriptionId = updated.id;
      } else {
        // Create new subscription
        const subscription = await this.stripeService.createSubscription(
          customerId,
          priceId,
          {
            trialDays: newPlan.trialDays,
            metadata: { tenantId, plan: newTier },
          },
        );
        subscriptionId = subscription.id;
      }
    }

    // Update tenant plan
    const tenant = await this.prisma.tenant.findUnique({ where: { id: tenantId } });
    const existingMetadata = (tenant?.metadata as Record<string, unknown>) || {};

    await this.prisma.tenant.update({
      where: { id: tenantId },
      data: {
        metadata: {
          ...existingMetadata,
          plan: newTier,
          planChangedAt: new Date().toISOString(),
          billingCycle,
          stripeSubscriptionId: subscriptionId || existingMetadata.stripeSubscriptionId,
        } as any,
      },
    });

    // Invalidate cache
    await this.redisService.del(`tenant:plan:${tenantId}`);

    const result: PlanChangeResult = {
      previousPlan: currentTier,
      newPlan: newTier,
      isUpgrade,
      effectiveDate: new Date().toISOString(),
      prorationAmount,
      subscriptionId,
    };

    this.logger.log(
      `Tenant ${tenantId} changed plan: ${currentTier} -> ${newTier} (${isUpgrade ? 'upgrade' : 'downgrade'})`,
    );

    return result;
  }

  // ---------- Trial Management ----------

  async startTrial(tenantId: string, tier: PlanTier): Promise<{ trialEndsAt: string }> {
    const plan = this.getPlan(tier);
    if (plan.trialDays <= 0) {
      throw new BadRequestException(`No trial available for ${tier} plan`);
    }

    const trialEndsAt = new Date(Date.now() + plan.trialDays * 86400000);

    const tenant = await this.prisma.tenant.findUnique({ where: { id: tenantId } });
    const existingMetadata = (tenant?.metadata as Record<string, unknown>) || {};

    // Check if already had a trial
    if (existingMetadata.trialUsed) {
      throw new BadRequestException('Trial already used for this tenant');
    }

    await this.prisma.tenant.update({
      where: { id: tenantId },
      data: {
        metadata: {
          ...existingMetadata,
          plan: tier,
          trialEndsAt: trialEndsAt.toISOString(),
          trialStartedAt: new Date().toISOString(),
          trialUsed: true,
          isTrial: true,
        } as any,
      },
    });

    await this.redisService.del(`tenant:plan:${tenantId}`);

    this.logger.log(
      `Started ${plan.trialDays}-day trial of ${tier} for tenant ${tenantId}`,
    );

    return { trialEndsAt: trialEndsAt.toISOString() };
  }

  async isTrialExpired(tenantId: string): Promise<boolean> {
    const tenant = await this.prisma.tenant.findUnique({ where: { id: tenantId } });
    const metadata = (tenant?.metadata as Record<string, unknown>) || {};

    if (!metadata.isTrial) return false;

    const trialEndsAt = metadata.trialEndsAt as string;
    if (!trialEndsAt) return false;

    return new Date(trialEndsAt) < new Date();
  }

  // ---------- Feature Checks ----------

  async hasFeature(tenantId: string, featureName: string): Promise<boolean> {
    const tier = await this.getTenantPlan(tenantId);
    const plan = this.getPlan(tier);
    const feature = plan.features.find((f) => f.name === featureName);
    return feature?.included ?? false;
  }

  async checkResourceLimit(
    tenantId: string,
    resourceType: string,
    currentCount: number,
  ): Promise<boolean> {
    const tier = await this.getTenantPlan(tenantId);
    const limits = this.getPlanLimits(tier);
    const limit = limits[resourceType];

    if (limit === undefined || limit === -1) return true; // Unlimited
    return currentCount < limit;
  }
}
