'use client';

import Link from 'next/link';
import { Card, CardContent } from '@/components/ui/card';
import {
  Settings,
  Users,
  Plug,
  CreditCard,
  Phone,
  ShieldCheck,
  ChevronRight,
} from 'lucide-react';

const settingsPages = [
  { title: 'General', description: 'Company info, timezone, preferences', icon: Settings, href: '/settings/general' },
  { title: 'Team', description: 'Members, roles, and invitations', icon: Users, href: '/settings/team' },
  { title: 'Integrations', description: 'Connect CRM, webhooks, and APIs', icon: Plug, href: '/settings/integrations' },
  { title: 'Billing & Usage', description: 'Plan, usage meters, and invoices', icon: CreditCard, href: '/settings/billing' },
  { title: 'Phone Numbers', description: 'Manage phone numbers and routing', icon: Phone, href: '/settings/phone-numbers' },
  { title: 'Compliance', description: 'Data retention, consent, and regulations', icon: ShieldCheck, href: '/settings/compliance' },
];

export default function SettingsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">Settings</h2>
        <p className="text-sm text-muted-foreground">
          Manage your workspace configuration
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {settingsPages.map((page) => (
          <Link key={page.href} href={page.href}>
            <Card className="group transition-all hover:shadow-md hover:border-primary/20">
              <CardContent className="flex items-center gap-4 p-5">
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
                  <page.icon className="h-5 w-5 text-primary" />
                </div>
                <div className="flex-1">
                  <h3 className="font-semibold group-hover:text-primary transition-colors">{page.title}</h3>
                  <p className="text-xs text-muted-foreground">{page.description}</p>
                </div>
                <ChevronRight className="h-4 w-4 text-muted-foreground group-hover:text-primary transition-colors" />
              </CardContent>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  );
}
