'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { Phone, PhoneIncoming, PhoneOutgoing } from 'lucide-react';
import Link from 'next/link';

const mockCalls = [
  { id: '1', contact: 'Sarah Johnson', phone: '+1 (555) 123-4567', agent: 'Sales Bot A', duration: '3:42', status: 'completed', outcome: 'Interested', direction: 'outbound', time: '2 min ago' },
  { id: '2', contact: 'Michael Chen', phone: '+1 (555) 234-5678', agent: 'Support Bot', duration: '1:15', status: 'completed', outcome: 'Resolved', direction: 'inbound', time: '8 min ago' },
  { id: '3', contact: 'Emily Davis', phone: '+1 (555) 345-6789', agent: 'Sales Bot B', duration: '0:00', status: 'no-answer', outcome: 'No Answer', direction: 'outbound', time: '12 min ago' },
  { id: '4', contact: 'James Wilson', phone: '+1 (555) 456-7890', agent: 'Sales Bot A', duration: '5:18', status: 'completed', outcome: 'Callback', direction: 'outbound', time: '18 min ago' },
  { id: '5', contact: 'Lisa Anderson', phone: '+1 (555) 567-8901', agent: 'Booking Bot', duration: '2:45', status: 'completed', outcome: 'Booked', direction: 'inbound', time: '25 min ago' },
];

const outcomeColors: Record<string, 'default' | 'success' | 'warning' | 'destructive' | 'secondary'> = {
  Interested: 'success',
  Resolved: 'success',
  Booked: 'success',
  'No Answer': 'secondary',
  Callback: 'warning',
  'Not Interested': 'destructive',
};

export function RecentCallsTable() {
  return (
    <Card className="col-span-full">
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-base font-semibold">Recent Calls</CardTitle>
        <Link href="/calls" className="text-sm text-primary hover:underline">
          View all
        </Link>
      </CardHeader>
      <CardContent className="pt-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-8"></TableHead>
              <TableHead>Contact</TableHead>
              <TableHead>Agent</TableHead>
              <TableHead>Duration</TableHead>
              <TableHead>Outcome</TableHead>
              <TableHead className="text-right">Time</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {mockCalls.map((call) => (
              <TableRow key={call.id} className="cursor-pointer">
                <TableCell>
                  {call.direction === 'outbound' ? (
                    <PhoneOutgoing className="h-3.5 w-3.5 text-blue-500" />
                  ) : (
                    <PhoneIncoming className="h-3.5 w-3.5 text-green-500" />
                  )}
                </TableCell>
                <TableCell>
                  <div>
                    <p className="font-medium">{call.contact}</p>
                    <p className="text-xs text-muted-foreground">{call.phone}</p>
                  </div>
                </TableCell>
                <TableCell className="text-muted-foreground">{call.agent}</TableCell>
                <TableCell className="font-mono text-sm">{call.duration}</TableCell>
                <TableCell>
                  <Badge variant={outcomeColors[call.outcome] || 'secondary'}>
                    {call.outcome}
                  </Badge>
                </TableCell>
                <TableCell className="text-right text-sm text-muted-foreground">{call.time}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
