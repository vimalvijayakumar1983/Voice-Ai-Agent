'use client';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Plus, Mail, MoreVertical } from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';

const teamMembers = [
  { name: 'John Smith', email: 'john@techcorp.com', role: 'Admin', status: 'active', initials: 'JS' },
  { name: 'Sarah Johnson', email: 'sarah@techcorp.com', role: 'Manager', status: 'active', initials: 'SJ' },
  { name: 'Michael Chen', email: 'mchen@techcorp.com', role: 'Agent', status: 'active', initials: 'MC' },
  { name: 'Emily Davis', email: 'emily@techcorp.com', role: 'Viewer', status: 'active', initials: 'ED' },
  { name: 'Robert Brown', email: 'rbrown@techcorp.com', role: 'Agent', status: 'invited', initials: 'RB' },
];

export default function TeamSettingsPage() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">Team Management</h2>
          <p className="text-sm text-muted-foreground">
            Manage team members and their permissions
          </p>
        </div>
        <Button>
          <Plus className="mr-2 h-4 w-4" />
          Invite Member
        </Button>
      </div>

      {/* Invite form */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Invite Team Member</CardTitle>
          <CardDescription>Send an invitation to join your workspace.</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex items-end gap-3">
            <div className="flex-1 space-y-2">
              <Input type="email" placeholder="colleague@company.com" />
            </div>
            <Select defaultValue="agent">
              <SelectTrigger className="w-[140px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="admin">Admin</SelectItem>
                <SelectItem value="manager">Manager</SelectItem>
                <SelectItem value="agent">Agent</SelectItem>
                <SelectItem value="viewer">Viewer</SelectItem>
              </SelectContent>
            </Select>
            <Button>
              <Mail className="mr-2 h-4 w-4" />
              Send Invite
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Team list */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Team Members ({teamMembers.length})</CardTitle>
        </CardHeader>
        <CardContent className="space-y-1">
          {teamMembers.map((member) => (
            <div key={member.email} className="flex items-center justify-between rounded-lg p-3 hover:bg-muted/50">
              <div className="flex items-center gap-3">
                <Avatar className="h-9 w-9">
                  <AvatarFallback className="bg-primary/10 text-xs text-primary">
                    {member.initials}
                  </AvatarFallback>
                </Avatar>
                <div>
                  <div className="flex items-center gap-2">
                    <p className="text-sm font-medium">{member.name}</p>
                    {member.status === 'invited' && (
                      <Badge variant="warning" className="text-[10px]">Invited</Badge>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground">{member.email}</p>
                </div>
              </div>
              <div className="flex items-center gap-3">
                <Badge variant="outline">{member.role}</Badge>
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button variant="ghost" size="icon" className="h-8 w-8">
                      <MoreVertical className="h-4 w-4" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuItem>Change Role</DropdownMenuItem>
                    <DropdownMenuItem>View Activity</DropdownMenuItem>
                    <DropdownMenuItem className="text-destructive">Remove</DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
