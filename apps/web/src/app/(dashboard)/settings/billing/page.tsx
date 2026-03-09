'use client';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { CreditCard, Download, Zap } from 'lucide-react';

export default function BillingPage() {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">Billing & Usage</h2>
        <p className="text-sm text-muted-foreground">
          Manage your subscription and monitor usage
        </p>
      </div>

      {/* Current Plan */}
      <Card className="border-primary/30 bg-primary/5">
        <CardContent className="p-6">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-4">
              <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-primary/10">
                <Zap className="h-6 w-6 text-primary" />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <h3 className="text-lg font-bold">Enterprise Plan</h3>
                  <Badge>Current</Badge>
                </div>
                <p className="text-sm text-muted-foreground">$2,499/month &middot; Billed annually</p>
              </div>
            </div>
            <div className="flex gap-2">
              <Button variant="outline">Change Plan</Button>
              <Button variant="outline">Manage Billing</Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Usage Meters */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Usage This Month</CardTitle>
          <CardDescription>Mar 1 - Mar 31, 2024</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {[
            { label: 'Voice Minutes', used: 12450, limit: 50000, unit: 'min' },
            { label: 'AI Agent Calls', used: 8920, limit: 25000, unit: 'calls' },
            { label: 'Concurrent Channels', used: 5, limit: 20, unit: 'channels' },
            { label: 'Team Members', used: 5, limit: 25, unit: 'seats' },
            { label: 'Storage', used: 2.4, limit: 10, unit: 'GB' },
          ].map((meter) => (
            <div key={meter.label} className="space-y-2">
              <div className="flex items-center justify-between text-sm">
                <span className="font-medium">{meter.label}</span>
                <span className="text-muted-foreground">
                  {meter.used.toLocaleString()} / {meter.limit.toLocaleString()} {meter.unit}
                </span>
              </div>
              <div className="h-2 rounded-full bg-secondary">
                <div
                  className={`h-full rounded-full transition-all ${
                    (meter.used / meter.limit) > 0.8 ? 'bg-orange-500' : 'bg-primary'
                  }`}
                  style={{ width: `${Math.min(100, (meter.used / meter.limit) * 100)}%` }}
                />
              </div>
              <p className="text-[11px] text-muted-foreground">
                {Math.round((meter.used / meter.limit) * 100)}% used
              </p>
            </div>
          ))}
        </CardContent>
      </Card>

      {/* Payment Method */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Payment Method</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <CreditCard className="h-5 w-5 text-muted-foreground" />
              <div>
                <p className="text-sm font-medium">Visa ending in 4242</p>
                <p className="text-xs text-muted-foreground">Expires 12/2025</p>
              </div>
            </div>
            <Button variant="outline" size="sm">Update</Button>
          </div>
        </CardContent>
      </Card>

      {/* Invoices */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-base">Invoices</CardTitle>
          <Button variant="outline" size="sm">
            <Download className="mr-2 h-4 w-4" />
            Download All
          </Button>
        </CardHeader>
        <CardContent className="space-y-2">
          {[
            { date: 'Mar 1, 2024', amount: '$2,499.00', status: 'Pending' },
            { date: 'Feb 1, 2024', amount: '$2,499.00', status: 'Paid' },
            { date: 'Jan 1, 2024', amount: '$2,499.00', status: 'Paid' },
            { date: 'Dec 1, 2023', amount: '$2,499.00', status: 'Paid' },
          ].map((invoice) => (
            <div key={invoice.date} className="flex items-center justify-between rounded-lg p-3 hover:bg-muted/50">
              <div>
                <p className="text-sm font-medium">{invoice.date}</p>
                <p className="text-xs text-muted-foreground">{invoice.amount}</p>
              </div>
              <div className="flex items-center gap-3">
                <Badge variant={invoice.status === 'Paid' ? 'success' : 'warning'}>
                  {invoice.status}
                </Badge>
                <Button variant="ghost" size="sm">
                  <Download className="h-4 w-4" />
                </Button>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
