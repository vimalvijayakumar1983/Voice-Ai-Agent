'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Building, Users, Phone, Activity, Server, Database, Cpu, HardDrive } from 'lucide-react';

export default function AdminPage() {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">Admin Panel</h2>
        <p className="text-sm text-muted-foreground">
          System-wide overview and management
        </p>
      </div>

      {/* System KPIs */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {[
          { title: 'Total Tenants', value: '127', change: '+3 this week', icon: Building, color: 'text-blue-600 dark:text-blue-400', bg: 'bg-blue-100 dark:bg-blue-900/30' },
          { title: 'Total Users', value: '1,842', change: '+28 this week', icon: Users, color: 'text-green-600 dark:text-green-400', bg: 'bg-green-100 dark:bg-green-900/30' },
          { title: 'Active Calls', value: '234', change: 'Right now', icon: Phone, color: 'text-orange-600 dark:text-orange-400', bg: 'bg-orange-100 dark:bg-orange-900/30' },
          { title: 'System Health', value: '99.9%', change: 'Last 30 days', icon: Activity, color: 'text-emerald-600 dark:text-emerald-400', bg: 'bg-emerald-100 dark:bg-emerald-900/30' },
        ].map((kpi) => (
          <Card key={kpi.title}>
            <CardContent className="p-5">
              <div className="flex items-start justify-between">
                <div>
                  <p className="text-sm text-muted-foreground">{kpi.title}</p>
                  <p className="mt-1 text-2xl font-bold">{kpi.value}</p>
                  <p className="mt-0.5 text-xs text-muted-foreground">{kpi.change}</p>
                </div>
                <div className={`rounded-xl p-3 ${kpi.bg}`}>
                  <kpi.icon className={`h-5 w-5 ${kpi.color}`} />
                </div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* System Health */}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Infrastructure Status</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {[
              { name: 'API Servers', status: 'Operational', icon: Server },
              { name: 'Database Cluster', status: 'Operational', icon: Database },
              { name: 'Voice Processing', status: 'Operational', icon: Cpu },
              { name: 'Storage', status: 'Degraded', icon: HardDrive },
            ].map((service) => (
              <div key={service.name} className="flex items-center justify-between rounded-lg border p-3">
                <div className="flex items-center gap-3">
                  <service.icon className="h-4 w-4 text-muted-foreground" />
                  <span className="text-sm font-medium">{service.name}</span>
                </div>
                <Badge variant={service.status === 'Operational' ? 'success' : 'warning'}>
                  {service.status}
                </Badge>
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">Top Tenants by Usage</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {[
              { name: 'TechCorp Solutions', calls: 45200, plan: 'Enterprise' },
              { name: 'MedGroup Inc', calls: 32100, plan: 'Enterprise' },
              { name: 'StartupXYZ', calls: 18900, plan: 'Growth' },
              { name: 'BigCo Industries', calls: 15600, plan: 'Enterprise' },
              { name: 'Innovate.io', calls: 12400, plan: 'Growth' },
            ].map((tenant) => (
              <div key={tenant.name} className="flex items-center justify-between rounded-lg border p-3">
                <div>
                  <p className="text-sm font-medium">{tenant.name}</p>
                  <p className="text-xs text-muted-foreground">{tenant.calls.toLocaleString()} calls this month</p>
                </div>
                <Badge variant="outline">{tenant.plan}</Badge>
              </div>
            ))}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
