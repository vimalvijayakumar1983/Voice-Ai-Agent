'use client';

import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { DataTable, type Column } from '@/components/ui/data-table';
import { Plus, MoreVertical } from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';

interface Tenant {
  id: string;
  name: string;
  plan: string;
  status: string;
  users: number;
  agents: number;
  callsThisMonth: number;
  createdAt: string;
}

const mockTenants: Tenant[] = [
  { id: '1', name: 'TechCorp Solutions', plan: 'Enterprise', status: 'active', users: 25, agents: 8, callsThisMonth: 45200, createdAt: 'Jan 2024' },
  { id: '2', name: 'MedGroup Inc', plan: 'Enterprise', status: 'active', users: 18, agents: 5, callsThisMonth: 32100, createdAt: 'Feb 2024' },
  { id: '3', name: 'StartupXYZ', plan: 'Growth', status: 'active', users: 6, agents: 3, callsThisMonth: 18900, createdAt: 'Mar 2024' },
  { id: '4', name: 'BigCo Industries', plan: 'Enterprise', status: 'active', users: 42, agents: 12, callsThisMonth: 15600, createdAt: 'Nov 2023' },
  { id: '5', name: 'Innovate.io', plan: 'Growth', status: 'trial', users: 3, agents: 2, callsThisMonth: 12400, createdAt: 'Mar 2024' },
  { id: '6', name: 'SmallBiz Co', plan: 'Starter', status: 'suspended', users: 2, agents: 1, callsThisMonth: 0, createdAt: 'Dec 2023' },
];

const columns: Column<Tenant>[] = [
  { key: 'name', header: 'Tenant', cell: (row) => <span className="font-medium">{row.name}</span> },
  { key: 'plan', header: 'Plan', cell: (row) => <Badge variant="outline">{row.plan}</Badge> },
  {
    key: 'status',
    header: 'Status',
    cell: (row) => (
      <Badge
        variant={
          row.status === 'active' ? 'success' : row.status === 'trial' ? 'warning' : 'destructive'
        }
      >
        {row.status}
      </Badge>
    ),
  },
  { key: 'users', header: 'Users', cell: (row) => row.users },
  { key: 'agents', header: 'Agents', cell: (row) => row.agents },
  { key: 'callsThisMonth', header: 'Calls (Month)', cell: (row) => row.callsThisMonth.toLocaleString() },
  { key: 'createdAt', header: 'Created', cell: (row) => row.createdAt },
  {
    key: 'actions',
    header: '',
    cell: () => (
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="icon" className="h-8 w-8">
            <MoreVertical className="h-4 w-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem>View Details</DropdownMenuItem>
          <DropdownMenuItem>Impersonate</DropdownMenuItem>
          <DropdownMenuItem>Edit Plan</DropdownMenuItem>
          <DropdownMenuItem className="text-destructive">Suspend</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    ),
    className: 'w-8',
  },
];

export default function TenantsPage() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">Tenant Management</h2>
          <p className="text-sm text-muted-foreground">
            Manage all tenants on the platform
          </p>
        </div>
        <Button>
          <Plus className="mr-2 h-4 w-4" />
          Create Tenant
        </Button>
      </div>

      <DataTable
        columns={columns}
        data={mockTenants}
        searchPlaceholder="Search tenants..."
        onSearch={() => {}}
        page={1}
        totalPages={3}
        onPageChange={() => {}}
      />
    </div>
  );
}
