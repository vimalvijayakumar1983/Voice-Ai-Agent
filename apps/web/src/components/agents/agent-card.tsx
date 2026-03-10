'use client';

import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { Bot, Phone, PhoneCall, BarChart3, MoreVertical } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import Link from 'next/link';

interface AgentCardProps {
  id: string;
  name: string;
  type: string;
  industry: string;
  status: 'active' | 'inactive' | 'draft';
  callCount: number;
  successRate: number;
  language: string;
  onDemoCall?: (agentId: string, agentName: string) => void;
}

const statusVariant: Record<string, 'success' | 'secondary' | 'warning'> = {
  active: 'success',
  inactive: 'secondary',
  draft: 'warning',
};

const typeIcons: Record<string, string> = {
  'outbound-sales': '📞',
  'inbound-support': '🎧',
  'appointment-booking': '📅',
  survey: '📋',
  'lead-qualification': '🎯',
  collections: '💰',
  custom: '⚙️',
};

export function AgentCard({
  id,
  name,
  type,
  industry,
  status,
  callCount,
  successRate,
  language,
  onDemoCall,
}: AgentCardProps) {
  return (
    <Link href={`/agents/${id}`}>
      <Card className="group transition-all hover:shadow-md hover:border-primary/20">
        <CardContent className="p-5">
          <div className="flex items-start justify-between">
            <div className="flex items-center gap-3">
              <Avatar className="h-10 w-10 rounded-lg">
                <AvatarFallback className="rounded-lg bg-primary/10 text-lg">
                  {typeIcons[type] || '🤖'}
                </AvatarFallback>
              </Avatar>
              <div>
                <h3 className="font-semibold group-hover:text-primary transition-colors">{name}</h3>
                <p className="text-xs text-muted-foreground capitalize">{type.replace(/-/g, ' ')}</p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Badge variant={statusVariant[status]}>{status}</Badge>
              <DropdownMenu>
                <DropdownMenuTrigger asChild onClick={(e) => e.preventDefault()}>
                  <Button variant="ghost" size="icon" className="h-8 w-8">
                    <MoreVertical className="h-4 w-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  {status === 'active' && (
                    <DropdownMenuItem onClick={(e) => { e.preventDefault(); onDemoCall?.(id, name); }}>
                      <PhoneCall className="mr-2 h-4 w-4" />
                      Demo Call
                    </DropdownMenuItem>
                  )}
                  <DropdownMenuItem>Edit</DropdownMenuItem>
                  <DropdownMenuItem>Duplicate</DropdownMenuItem>
                  <DropdownMenuItem>View Calls</DropdownMenuItem>
                  <DropdownMenuItem className="text-destructive">Deactivate</DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          </div>

          <div className="mt-4 flex items-center gap-4 text-sm">
            <div className="flex items-center gap-1.5 text-muted-foreground">
              <Phone className="h-3.5 w-3.5" />
              <span>{callCount.toLocaleString()} calls</span>
            </div>
            <div className="flex items-center gap-1.5 text-muted-foreground">
              <BarChart3 className="h-3.5 w-3.5" />
              <span>{successRate}% success</span>
            </div>
          </div>

          <div className="mt-3 flex items-center gap-2">
            <Badge variant="outline" className="text-[10px]">{industry}</Badge>
            <Badge variant="outline" className="text-[10px]">{language}</Badge>
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}
