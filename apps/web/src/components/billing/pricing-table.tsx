'use client';

import { useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle, CardDescription, CardFooter } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Check, X, Loader2, Sparkles } from 'lucide-react';
import { cn, formatCurrency } from '@/lib/utils';

// ---------- Types ----------

interface PlanDefinition {
  tier: string;
  name: string;
  description: string;
  monthlyPrice: number;
  annualPrice: number;
  features: Array<{ name: string; included: boolean; limit?: string }>;
  highlighted?: boolean;
}

interface PricingTableProps {
  plans: PlanDefinition[];
  currentPlan?: string;
  billingCycle: 'monthly' | 'annual';
  onBillingCycleChange: (cycle: 'monthly' | 'annual') => void;
  onSelectPlan: (tier: string, billingCycle: 'monthly' | 'annual') => Promise<void>;
  isLoading?: boolean;
  className?: string;
}

// ---------- Component ----------

export function PricingTable({
  plans,
  currentPlan,
  billingCycle,
  onBillingCycleChange,
  onSelectPlan,
  isLoading = false,
  className,
}: PricingTableProps) {
  const [selectingPlan, setSelectingPlan] = useState<string | null>(null);

  const handleSelectPlan = async (tier: string) => {
    if (tier === currentPlan) return;

    setSelectingPlan(tier);
    try {
      await onSelectPlan(tier, billingCycle);
    } catch (error) {
      console.error('Plan selection failed:', error);
    } finally {
      setSelectingPlan(null);
    }
  };

  const annualSavings = (monthly: number, annual: number) => {
    if (monthly === 0) return 0;
    const monthlyCost = monthly * 12;
    return Math.round(((monthlyCost - annual) / monthlyCost) * 100);
  };

  return (
    <div className={cn('space-y-6', className)}>
      {/* Billing Cycle Toggle */}
      <div className="flex items-center justify-center gap-3">
        <span
          className={cn(
            'text-sm font-medium transition-colors cursor-pointer',
            billingCycle === 'monthly' ? 'text-foreground' : 'text-muted-foreground',
          )}
          onClick={() => onBillingCycleChange('monthly')}
        >
          Monthly
        </span>
        <button
          className={cn(
            'relative inline-flex h-6 w-11 items-center rounded-full transition-colors',
            billingCycle === 'annual' ? 'bg-primary' : 'bg-muted',
          )}
          onClick={() =>
            onBillingCycleChange(billingCycle === 'monthly' ? 'annual' : 'monthly')
          }
        >
          <span
            className={cn(
              'inline-block h-4 w-4 transform rounded-full bg-white transition-transform',
              billingCycle === 'annual' ? 'translate-x-6' : 'translate-x-1',
            )}
          />
        </button>
        <span
          className={cn(
            'text-sm font-medium transition-colors cursor-pointer',
            billingCycle === 'annual' ? 'text-foreground' : 'text-muted-foreground',
          )}
          onClick={() => onBillingCycleChange('annual')}
        >
          Annual
        </span>
        {billingCycle === 'annual' && (
          <Badge variant="success" className="text-xs">
            Save up to 20%
          </Badge>
        )}
      </div>

      {/* Plans Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {plans.map((plan) => {
          const isCurrent = plan.tier === currentPlan;
          const price =
            billingCycle === 'annual'
              ? plan.annualPrice / 12
              : plan.monthlyPrice;
          const savings = annualSavings(plan.monthlyPrice, plan.annualPrice);
          const isHighlighted = plan.highlighted;

          return (
            <Card
              key={plan.tier}
              className={cn(
                'relative flex flex-col',
                isHighlighted && 'border-primary shadow-lg scale-[1.02]',
                isCurrent && 'ring-2 ring-primary',
              )}
            >
              {isHighlighted && (
                <div className="absolute -top-3 left-1/2 -translate-x-1/2">
                  <Badge className="flex items-center gap-1">
                    <Sparkles className="h-3 w-3" />
                    Most Popular
                  </Badge>
                </div>
              )}

              {isCurrent && (
                <div className="absolute -top-3 right-4">
                  <Badge variant="outline">Current Plan</Badge>
                </div>
              )}

              <CardHeader className="pb-4">
                <CardTitle className="text-xl">{plan.name}</CardTitle>
                <CardDescription className="text-xs min-h-[2.5rem]">
                  {plan.description}
                </CardDescription>
              </CardHeader>

              <CardContent className="flex-1 space-y-4">
                {/* Price */}
                <div>
                  <div className="flex items-baseline gap-1">
                    <span className="text-3xl font-bold">
                      {price === 0 ? 'Free' : formatCurrency(price)}
                    </span>
                    {price > 0 && (
                      <span className="text-sm text-muted-foreground">/mo</span>
                    )}
                  </div>
                  {billingCycle === 'annual' && price > 0 && (
                    <p className="text-xs text-muted-foreground mt-1">
                      {formatCurrency(plan.annualPrice)}/year
                      {savings > 0 && (
                        <span className="text-green-600 dark:text-green-400 ml-1">
                          (save {savings}%)
                        </span>
                      )}
                    </p>
                  )}
                </div>

                {/* Features */}
                <ul className="space-y-2">
                  {plan.features.map((feature, i) => (
                    <li key={i} className="flex items-start gap-2 text-sm">
                      {feature.included ? (
                        <Check className="h-4 w-4 text-green-600 dark:text-green-400 flex-shrink-0 mt-0.5" />
                      ) : (
                        <X className="h-4 w-4 text-muted-foreground/40 flex-shrink-0 mt-0.5" />
                      )}
                      <span
                        className={cn(
                          !feature.included && 'text-muted-foreground/60',
                        )}
                      >
                        {feature.name}
                      </span>
                    </li>
                  ))}
                </ul>
              </CardContent>

              <CardFooter>
                <Button
                  className="w-full"
                  variant={isHighlighted ? 'default' : 'outline'}
                  disabled={isCurrent || isLoading || selectingPlan !== null}
                  onClick={() => handleSelectPlan(plan.tier)}
                >
                  {selectingPlan === plan.tier ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : null}
                  {isCurrent
                    ? 'Current Plan'
                    : currentPlan &&
                        plans.findIndex((p) => p.tier === currentPlan) >
                          plans.findIndex((p) => p.tier === plan.tier)
                      ? 'Downgrade'
                      : plan.monthlyPrice === 0
                        ? 'Get Started'
                        : 'Upgrade'}
                </Button>
              </CardFooter>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
