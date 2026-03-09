'use client';

import { useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { DataTable, type Column } from '@/components/ui/data-table';
import { CallStatusBadge } from '@/components/calls/call-status-badge';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Input } from '@/components/ui/input';
import { PhoneOutgoing, PhoneIncoming, Download, Filter } from 'lucide-react';

interface CallRecord {
  id: string;
  direction: 'outbound' | 'inbound';
  contact: string;
  phone: string;
  agent: string;
  campaign: string;
  status: string;
  outcome: string;
  duration: string;
  date: string;
}

const mockCalls: CallRecord[] = [
  { id: '1', direction: 'outbound', contact: 'Sarah Johnson', phone: '+1 (555) 123-4567', agent: 'Sales Bot A', campaign: 'Q1 Lead Gen', status: 'completed', outcome: 'Interested', duration: '3:42', date: '10 min ago' },
  { id: '2', direction: 'inbound', contact: 'Michael Chen', phone: '+1 (555) 234-5678', agent: 'Support Bot', campaign: '-', status: 'completed', outcome: 'Resolved', duration: '1:15', date: '25 min ago' },
  { id: '3', direction: 'outbound', contact: 'Emily Davis', phone: '+1 (555) 345-6789', agent: 'Sales Bot B', campaign: 'Q1 Lead Gen', status: 'no-answer', outcome: 'No Answer', duration: '0:00', date: '32 min ago' },
  { id: '4', direction: 'outbound', contact: 'James Wilson', phone: '+1 (555) 456-7890', agent: 'Sales Bot A', campaign: 'Re-engagement', status: 'completed', outcome: 'Callback', duration: '5:18', date: '45 min ago' },
  { id: '5', direction: 'inbound', contact: 'Lisa Anderson', phone: '+1 (555) 567-8901', agent: 'Booking Bot', campaign: '-', status: 'completed', outcome: 'Booked', duration: '2:45', date: '1 hour ago' },
  { id: '6', direction: 'outbound', contact: 'Robert Brown', phone: '+1 (555) 678-9012', agent: 'Sales Bot A', campaign: 'Q1 Lead Gen', status: 'failed', outcome: 'Error', duration: '0:00', date: '1.5 hours ago' },
  { id: '7', direction: 'outbound', contact: 'Jennifer Lee', phone: '+1 (555) 789-0123', agent: 'Sales Bot B', campaign: 'Q1 Lead Gen', status: 'completed', outcome: 'Not Interested', duration: '1:02', date: '2 hours ago' },
  { id: '8', direction: 'outbound', contact: 'David Martinez', phone: '+1 (555) 890-1234', agent: 'Collections Bot', campaign: 'Collections', status: 'completed', outcome: 'Promise to Pay', duration: '4:30', date: '2.5 hours ago' },
];

const columns: Column<CallRecord>[] = [
  {
    key: 'direction',
    header: '',
    cell: (row) =>
      row.direction === 'outbound' ? (
        <PhoneOutgoing className="h-3.5 w-3.5 text-blue-500" />
      ) : (
        <PhoneIncoming className="h-3.5 w-3.5 text-green-500" />
      ),
    className: 'w-8',
  },
  {
    key: 'contact',
    header: 'Contact',
    cell: (row) => (
      <div>
        <p className="font-medium">{row.contact}</p>
        <p className="text-xs text-muted-foreground font-mono">{row.phone}</p>
      </div>
    ),
  },
  { key: 'agent', header: 'Agent', cell: (row) => <span className="text-muted-foreground">{row.agent}</span> },
  { key: 'campaign', header: 'Campaign', cell: (row) => <span className="text-muted-foreground">{row.campaign}</span> },
  { key: 'status', header: 'Status', cell: (row) => <CallStatusBadge status={row.status} /> },
  {
    key: 'outcome',
    header: 'Outcome',
    cell: (row) => {
      const variant = row.outcome === 'Interested' || row.outcome === 'Resolved' || row.outcome === 'Booked' || row.outcome === 'Promise to Pay'
        ? 'success'
        : row.outcome === 'Not Interested' || row.outcome === 'Error'
        ? 'destructive'
        : 'secondary';
      return <Badge variant={variant}>{row.outcome}</Badge>;
    },
  },
  { key: 'duration', header: 'Duration', cell: (row) => <span className="font-mono text-sm">{row.duration}</span> },
  { key: 'date', header: 'Time', cell: (row) => <span className="text-sm text-muted-foreground">{row.date}</span> },
];

export default function CallsPage() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">Call Log</h2>
          <p className="text-sm text-muted-foreground">
            View and manage all call records
          </p>
        </div>
        <Button variant="outline" size="sm">
          <Download className="mr-2 h-4 w-4" />
          Export
        </Button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3">
        <Select defaultValue="all">
          <SelectTrigger className="w-[150px]">
            <SelectValue placeholder="Status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Statuses</SelectItem>
            <SelectItem value="completed">Completed</SelectItem>
            <SelectItem value="in-progress">In Progress</SelectItem>
            <SelectItem value="failed">Failed</SelectItem>
            <SelectItem value="no-answer">No Answer</SelectItem>
          </SelectContent>
        </Select>
        <Select defaultValue="all">
          <SelectTrigger className="w-[150px]">
            <SelectValue placeholder="Agent" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Agents</SelectItem>
            <SelectItem value="sales-a">Sales Bot A</SelectItem>
            <SelectItem value="sales-b">Sales Bot B</SelectItem>
            <SelectItem value="support">Support Bot</SelectItem>
          </SelectContent>
        </Select>
        <Select defaultValue="all">
          <SelectTrigger className="w-[150px]">
            <SelectValue placeholder="Direction" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All</SelectItem>
            <SelectItem value="outbound">Outbound</SelectItem>
            <SelectItem value="inbound">Inbound</SelectItem>
          </SelectContent>
        </Select>
        <Input type="date" className="w-[160px]" />
      </div>

      <DataTable
        columns={columns}
        data={mockCalls}
        searchPlaceholder="Search calls..."
        onSearch={() => {}}
        onRowClick={(row) => (window.location.href = `/calls/${row.id}`)}
        page={1}
        totalPages={5}
        onPageChange={() => {}}
      />
    </div>
  );
}
