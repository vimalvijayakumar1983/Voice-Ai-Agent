'use client';

import { useState } from 'react';
import Link from 'next/link';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { DataTable, type Column } from '@/components/ui/data-table';
import { Plus, Upload, Download, Trash2 } from 'lucide-react';

interface Contact {
  id: string;
  name: string;
  email: string;
  phone: string;
  company: string;
  status: string;
  totalCalls: number;
  lastContact: string;
  tags: string[];
}

const mockContacts: Contact[] = [
  { id: '1', name: 'Sarah Johnson', email: 'sarah@techcorp.com', phone: '+1 (555) 123-4567', company: 'TechCorp', status: 'active', totalCalls: 5, lastContact: '2 hours ago', tags: ['hot-lead', 'enterprise'] },
  { id: '2', name: 'Michael Chen', email: 'mchen@innovate.io', phone: '+1 (555) 234-5678', company: 'Innovate.io', status: 'active', totalCalls: 3, lastContact: '1 day ago', tags: ['warm-lead'] },
  { id: '3', name: 'Emily Davis', email: 'emily@startupxyz.com', phone: '+1 (555) 345-6789', company: 'StartupXYZ', status: 'dnc', totalCalls: 1, lastContact: '5 days ago', tags: ['startup'] },
  { id: '4', name: 'James Wilson', email: 'jwilson@bigco.com', phone: '+1 (555) 456-7890', company: 'BigCo Inc', status: 'active', totalCalls: 8, lastContact: '3 hours ago', tags: ['enterprise', 'callback'] },
  { id: '5', name: 'Lisa Anderson', email: 'lisa@medgroup.com', phone: '+1 (555) 567-8901', company: 'MedGroup', status: 'active', totalCalls: 2, lastContact: '12 hours ago', tags: ['healthcare'] },
];

const columns: Column<Contact>[] = [
  {
    key: 'name',
    header: 'Contact',
    cell: (row) => (
      <div>
        <p className="font-medium">{row.name}</p>
        <p className="text-xs text-muted-foreground">{row.email}</p>
      </div>
    ),
  },
  { key: 'phone', header: 'Phone', cell: (row) => <span className="font-mono text-sm">{row.phone}</span> },
  { key: 'company', header: 'Company', cell: (row) => row.company },
  {
    key: 'status',
    header: 'Status',
    cell: (row) => (
      <Badge variant={row.status === 'active' ? 'success' : row.status === 'dnc' ? 'destructive' : 'secondary'}>
        {row.status === 'dnc' ? 'DNC' : row.status}
      </Badge>
    ),
  },
  { key: 'totalCalls', header: 'Calls', cell: (row) => row.totalCalls },
  { key: 'lastContact', header: 'Last Contact', cell: (row) => <span className="text-muted-foreground">{row.lastContact}</span> },
  {
    key: 'tags',
    header: 'Tags',
    cell: (row) => (
      <div className="flex flex-wrap gap-1">
        {row.tags.map((tag) => (
          <Badge key={tag} variant="outline" className="text-[10px]">{tag}</Badge>
        ))}
      </div>
    ),
  },
];

export default function ContactsPage() {
  const [selectedIds, setSelectedIds] = useState<string[]>([]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">Contacts</h2>
          <p className="text-sm text-muted-foreground">
            Manage your contact database ({mockContacts.length.toLocaleString()} contacts)
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm">
            <Upload className="mr-2 h-4 w-4" />
            Import
          </Button>
          <Button variant="outline" size="sm">
            <Download className="mr-2 h-4 w-4" />
            Export
          </Button>
          <Button size="sm">
            <Plus className="mr-2 h-4 w-4" />
            Add Contact
          </Button>
        </div>
      </div>

      <DataTable
        columns={columns}
        data={mockContacts}
        searchPlaceholder="Search contacts..."
        onSearch={() => {}}
        onRowClick={(row) => (window.location.href = `/contacts/${row.id}`)}
        page={1}
        totalPages={1}
      />
    </div>
  );
}
