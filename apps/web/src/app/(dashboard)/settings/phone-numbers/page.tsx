'use client';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Plus, Phone, MoreVertical } from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';

const phoneNumbers = [
  { number: '+1 (555) 100-0001', label: 'Primary Sales', type: 'Local', agent: 'Sales Bot A', status: 'active', callsToday: 145 },
  { number: '+1 (555) 100-0002', label: 'Sales Backup', type: 'Local', agent: 'Sales Bot B', status: 'active', callsToday: 89 },
  { number: '+1 (800) 555-0003', label: 'Support Line', type: 'Toll-free', agent: 'Support Bot', status: 'active', callsToday: 52 },
  { number: '+1 (555) 100-0004', label: 'Collections', type: 'Local', agent: 'Collections Bot', status: 'active', callsToday: 34 },
  { number: '+1 (555) 100-0005', label: 'Unused', type: 'Local', agent: 'Unassigned', status: 'inactive', callsToday: 0 },
];

export default function PhoneNumbersPage() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">Phone Numbers</h2>
          <p className="text-sm text-muted-foreground">
            Manage your phone numbers and routing
          </p>
        </div>
        <Button>
          <Plus className="mr-2 h-4 w-4" />
          Add Number
        </Button>
      </div>

      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Number</TableHead>
                <TableHead>Label</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Assigned Agent</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Calls Today</TableHead>
                <TableHead className="w-8"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {phoneNumbers.map((pn) => (
                <TableRow key={pn.number}>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <Phone className="h-4 w-4 text-muted-foreground" />
                      <span className="font-mono text-sm font-medium">{pn.number}</span>
                    </div>
                  </TableCell>
                  <TableCell>{pn.label}</TableCell>
                  <TableCell><Badge variant="outline">{pn.type}</Badge></TableCell>
                  <TableCell className="text-muted-foreground">{pn.agent}</TableCell>
                  <TableCell>
                    <Badge variant={pn.status === 'active' ? 'success' : 'secondary'}>{pn.status}</Badge>
                  </TableCell>
                  <TableCell>{pn.callsToday}</TableCell>
                  <TableCell>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button variant="ghost" size="icon" className="h-8 w-8">
                          <MoreVertical className="h-4 w-4" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem>Configure</DropdownMenuItem>
                        <DropdownMenuItem>Reassign</DropdownMenuItem>
                        <DropdownMenuItem className="text-destructive">Release</DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
