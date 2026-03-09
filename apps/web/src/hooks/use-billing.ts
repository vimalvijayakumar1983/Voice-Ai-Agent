'use client';

import { useMemo, useCallback } from 'react';
import { useApiQuery, useApiMutation } from './use-api';
import { useQueryClient, useMutation } from '@tanstack/react-query';
import api from '@/lib/api';

// ---------- Types ----------

interface PlanDefinition {
  tier: string;
  name: string;
  description: string;
  monthlyPrice: number;
  annualPrice: number;
  features: Array<{ name: string; included: boolean; limit?: string }>;
  highlighted?: boolean;
  limits: Record<string, number>;
}

interface UsageSummary {
  dimension: string;
  used: number;
  limit: number;
  percentage: number;
  unit: string;
}

interface Subscription {
  id?: string;
  plan: string;
  status: string;
  currentPeriodStart?: string;
  currentPeriodEnd?: string;
  cancelAtPeriodEnd?: boolean;
  trialEndsAt?: string;
  isTrial?: boolean;
}

interface Invoice {
  id: string;
  amount: number;
  currency: string;
  status: string;
  periodStart: string;
  periodEnd: string;
  hostedUrl?: string;
  pdfUrl?: string;
}

interface PlanChangeResult {
  previousPlan: string;
  newPlan: string;
  isUpgrade: boolean;
  effectiveDate: string;
  subscriptionId?: string;
}

interface PaginatedResponse<T> {
  data: T[];
  meta: {
    page: number;
    limit: number;
    totalItems: number;
    totalPages: number;
  };
}

// ---------- Subscription Hooks ----------

export function useSubscription() {
  return useApiQuery<Subscription>(
    ['billing', 'subscription'],
    '/billing/subscription',
  );
}

export function useChangePlan() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (variables: {
      tier: string;
      billingCycle: 'monthly' | 'annual';
    }) => {
      const { data } = await api.post<PlanChangeResult>(
        '/billing/change-plan',
        variables,
      );
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['billing', 'subscription'] });
      queryClient.invalidateQueries({ queryKey: ['billing', 'usage'] });
      queryClient.invalidateQueries({ queryKey: ['billing', 'plans'] });
    },
  });
}

export function useCancelSubscription() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (immediately: boolean = false) => {
      const { data } = await api.post('/billing/cancel', { immediately });
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['billing', 'subscription'] });
    },
  });
}

export function useReactivateSubscription() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async () => {
      const { data } = await api.post('/billing/reactivate');
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['billing', 'subscription'] });
    },
  });
}

// ---------- Usage Hooks ----------

export function useUsage() {
  return useApiQuery<UsageSummary[]>(
    ['billing', 'usage'],
    '/billing/usage',
    {
      refetchInterval: 60000, // Refresh every minute
    },
  );
}

export function useUsageHistory(
  dimension: string,
  startDate: string,
  endDate: string,
) {
  return useApiQuery<Array<{ date: string; quantity: number }>>(
    ['billing', 'usage-history', dimension, startDate, endDate],
    `/billing/usage/history?dimension=${dimension}&startDate=${startDate}&endDate=${endDate}`,
    {
      enabled: !!dimension && !!startDate && !!endDate,
    },
  );
}

// ---------- Plans Hooks ----------

export function usePlans() {
  return useApiQuery<PlanDefinition[]>(
    ['billing', 'plans'],
    '/billing/plans',
    {
      staleTime: 300000, // Cache for 5 minutes
    },
  );
}

export function usePlanComparison() {
  const { data: plans, ...rest } = usePlans();

  const comparison = useMemo(() => {
    if (!plans) return null;

    return {
      plans,
      features: extractAllFeatures(plans),
    };
  }, [plans]);

  return { data: comparison, ...rest };
}

// ---------- Invoice Hooks ----------

export function useInvoices(page = 1, limit = 10) {
  return useApiQuery<PaginatedResponse<Invoice>>(
    ['billing', 'invoices', String(page)],
    `/billing/invoices?page=${page}&limit=${limit}`,
  );
}

// ---------- Checkout & Portal ----------

export function useCreateCheckout() {
  return useMutation({
    mutationFn: async (variables: {
      priceId: string;
      successUrl?: string;
      cancelUrl?: string;
    }) => {
      const { data } = await api.post<{ url: string; sessionId: string }>(
        '/billing/checkout',
        variables,
      );
      return data;
    },
  });
}

export function useCreatePortalSession() {
  return useMutation({
    mutationFn: async (returnUrl?: string) => {
      const { data } = await api.post<{ url: string }>(
        '/billing/portal',
        { returnUrl },
      );
      return data;
    },
  });
}

// ---------- Payment Methods ----------

export function usePaymentMethods() {
  return useApiQuery<
    Array<{
      id: string;
      type: string;
      card?: { brand: string; last4: string; expMonth: number; expYear: number };
      isDefault: boolean;
    }>
  >(['billing', 'payment-methods'], '/billing/payment-methods');
}

// ---------- Combined Dashboard Hook ----------

export function useBillingDashboard() {
  const subscription = useSubscription();
  const usage = useUsage();
  const plans = usePlans();

  const isLoading =
    subscription.isLoading || usage.isLoading || plans.isLoading;
  const isError = subscription.isError || usage.isError || plans.isError;

  const currentPlan = useMemo(() => {
    if (!subscription.data || !plans.data) return null;
    return plans.data.find((p) => p.tier === subscription.data.plan) || null;
  }, [subscription.data, plans.data]);

  const billingPeriod = useMemo(() => {
    if (!subscription.data?.currentPeriodStart || !subscription.data?.currentPeriodEnd) {
      return null;
    }
    return {
      start: subscription.data.currentPeriodStart,
      end: subscription.data.currentPeriodEnd,
    };
  }, [subscription.data]);

  const usageWarnings = useMemo(() => {
    if (!usage.data) return [];
    return usage.data.filter((u) => u.percentage >= 80 && u.limit > 0);
  }, [usage.data]);

  return {
    subscription: subscription.data,
    usage: usage.data || [],
    plans: plans.data || [],
    currentPlan,
    billingPeriod,
    usageWarnings,
    isLoading,
    isError,
    refetch: () => {
      subscription.refetch();
      usage.refetch();
    },
  };
}

// ---------- Helpers ----------

function extractAllFeatures(
  plans: PlanDefinition[],
): Array<{ name: string; values: Record<string, string | boolean> }> {
  const featureNames = new Set<string>();
  for (const plan of plans) {
    for (const feature of plan.features) {
      featureNames.add(feature.name);
    }
  }

  return Array.from(featureNames).map((name) => {
    const values: Record<string, string | boolean> = {};
    for (const plan of plans) {
      const feature = plan.features.find((f) => f.name === name);
      values[plan.tier] = feature ? (feature.included ? feature.limit || true : false) : false;
    }
    return { name, values };
  });
}
