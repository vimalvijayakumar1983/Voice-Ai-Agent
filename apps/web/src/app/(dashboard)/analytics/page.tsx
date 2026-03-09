'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  LineChart,
  Line,
  BarChart,
  Bar,
  PieChart,
  Pie,
  Cell,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from 'recharts';

const callVolumeData = [
  { date: 'Jan', calls: 4200, connected: 2800 },
  { date: 'Feb', calls: 5800, connected: 3900 },
  { date: 'Mar', calls: 7200, connected: 5100 },
  { date: 'Apr', calls: 6800, connected: 4600 },
  { date: 'May', calls: 8500, connected: 6200 },
  { date: 'Jun', calls: 9100, connected: 6800 },
];

const outcomeData = [
  { name: 'Interested', value: 2800, color: '#10b981' },
  { name: 'Not Interested', value: 1500, color: '#ef4444' },
  { name: 'Callback', value: 900, color: '#f59e0b' },
  { name: 'No Answer', value: 1200, color: '#6b7280' },
  { name: 'Voicemail', value: 600, color: '#8b5cf6' },
];

const agentPerformanceData = [
  { agent: 'Sales Pro', calls: 4500, success: 72, avgDuration: 215 },
  { agent: 'Support Bot', calls: 3200, success: 91, avgDuration: 145 },
  { agent: 'Scheduler', calls: 2100, success: 78, avgDuration: 180 },
  { agent: 'Lead Qual', calls: 1800, success: 65, avgDuration: 195 },
  { agent: 'Collections', calls: 2400, success: 48, avgDuration: 240 },
];

const campaignComparisonData = [
  { name: 'Q1 Lead Gen', conversion: 24, connected: 62, satisfaction: 4.2 },
  { name: 'Re-engage', conversion: 18, connected: 58, satisfaction: 3.8 },
  { name: 'Holiday', conversion: 22, connected: 65, satisfaction: 4.0 },
  { name: 'Collections', conversion: 35, connected: 70, satisfaction: 3.2 },
];

const tooltipStyle = {
  backgroundColor: 'hsl(var(--card))',
  border: '1px solid hsl(var(--border))',
  borderRadius: '8px',
  fontSize: '12px',
};

export default function AnalyticsPage() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">Analytics</h2>
          <p className="text-sm text-muted-foreground">
            Deep insights into your voice AI performance
          </p>
        </div>
        <Select defaultValue="30d">
          <SelectTrigger className="w-[160px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="7d">Last 7 days</SelectItem>
            <SelectItem value="30d">Last 30 days</SelectItem>
            <SelectItem value="90d">Last 90 days</SelectItem>
            <SelectItem value="1y">Last year</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {/* Call Volume Over Time */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Call Volume Over Time</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="h-[350px]">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={callVolumeData}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="date" tick={{ fontSize: 12 }} tickLine={false} axisLine={false} />
                <YAxis tick={{ fontSize: 12 }} tickLine={false} axisLine={false} />
                <Tooltip contentStyle={tooltipStyle} />
                <Legend />
                <Line type="monotone" dataKey="calls" stroke="#3b82f6" strokeWidth={2} name="Total Calls" dot={false} />
                <Line type="monotone" dataKey="connected" stroke="#10b981" strokeWidth={2} name="Connected" dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* Outcomes Pie */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Call Outcomes Distribution</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex items-center gap-6">
              <div className="h-[250px] w-[250px]">
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie data={outcomeData} cx="50%" cy="50%" innerRadius={60} outerRadius={100} dataKey="value" strokeWidth={2} stroke="hsl(var(--card))">
                      {outcomeData.map((entry, i) => <Cell key={i} fill={entry.color} />)}
                    </Pie>
                    <Tooltip contentStyle={tooltipStyle} />
                  </PieChart>
                </ResponsiveContainer>
              </div>
              <div className="flex-1 space-y-3">
                {outcomeData.map((item) => (
                  <div key={item.name} className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="h-3 w-3 rounded-full" style={{ backgroundColor: item.color }} />
                      <span className="text-sm">{item.name}</span>
                    </div>
                    <span className="text-sm font-medium">{item.value.toLocaleString()}</span>
                  </div>
                ))}
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Agent Performance */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Agent Performance</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="h-[280px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={agentPerformanceData} layout="vertical">
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                  <XAxis type="number" tick={{ fontSize: 12 }} tickLine={false} axisLine={false} />
                  <YAxis dataKey="agent" type="category" tick={{ fontSize: 12 }} tickLine={false} axisLine={false} width={80} />
                  <Tooltip contentStyle={tooltipStyle} />
                  <Bar dataKey="success" name="Success %" fill="#3b82f6" radius={[0, 4, 4, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Campaign Comparison */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Campaign Comparison</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="h-[300px]">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={campaignComparisonData}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="name" tick={{ fontSize: 12 }} tickLine={false} axisLine={false} />
                <YAxis tick={{ fontSize: 12 }} tickLine={false} axisLine={false} />
                <Tooltip contentStyle={tooltipStyle} />
                <Legend />
                <Bar dataKey="conversion" name="Conversion %" fill="#3b82f6" radius={[4, 4, 0, 0]} />
                <Bar dataKey="connected" name="Connected %" fill="#10b981" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
