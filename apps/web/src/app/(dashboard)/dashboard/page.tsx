'use client';

import { Phone, PhoneCall, Clock, TrendingUp } from 'lucide-react';
import { KpiCard } from '@/components/dashboard/kpi-card';
import { CallVolumeChart } from '@/components/dashboard/call-volume-chart';
import { OutcomeDistribution } from '@/components/dashboard/outcome-distribution';
import { RecentCallsTable } from '@/components/dashboard/recent-calls-table';
import { ActiveCampaignsCard } from '@/components/dashboard/active-campaigns-card';

export default function DashboardPage() {
  return (
    <div className="space-y-6">
      {/* KPI Cards */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <KpiCard
          title="Total Calls Today"
          value="1,284"
          change={12.5}
          changeLabel="vs yesterday"
          icon={Phone}
          iconColor="text-blue-600 dark:text-blue-400"
          iconBg="bg-blue-100 dark:bg-blue-900/30"
        />
        <KpiCard
          title="Connected Calls"
          value="892"
          change={8.2}
          changeLabel="vs yesterday"
          icon={PhoneCall}
          iconColor="text-green-600 dark:text-green-400"
          iconBg="bg-green-100 dark:bg-green-900/30"
        />
        <KpiCard
          title="Avg Duration"
          value="3m 42s"
          change={-2.1}
          changeLabel="vs last week"
          icon={Clock}
          iconColor="text-orange-600 dark:text-orange-400"
          iconBg="bg-orange-100 dark:bg-orange-900/30"
        />
        <KpiCard
          title="Conversion Rate"
          value="24.8%"
          change={3.4}
          changeLabel="vs last week"
          icon={TrendingUp}
          iconColor="text-violet-600 dark:text-violet-400"
          iconBg="bg-violet-100 dark:bg-violet-900/30"
        />
      </div>

      {/* Charts row */}
      <div className="grid gap-6 lg:grid-cols-3">
        <CallVolumeChart />
        <OutcomeDistribution />
      </div>

      {/* Bottom section */}
      <div className="grid gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <RecentCallsTable />
        </div>
        <ActiveCampaignsCard />
      </div>
    </div>
  );
}
