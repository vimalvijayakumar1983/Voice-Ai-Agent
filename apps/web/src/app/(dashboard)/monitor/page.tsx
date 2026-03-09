'use client';

import { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { Activity, Phone, PhoneCall, Volume2, Eye, PauseCircle } from 'lucide-react';

const mockActiveCalls = [
  { id: '1', contact: 'Sarah Johnson', phone: '+1 (555) 123-4567', agent: 'Sales Bot A', duration: '2:15', status: 'connected', sentiment: 'positive' },
  { id: '2', contact: 'Michael Chen', phone: '+1 (555) 234-5678', agent: 'Support Bot', duration: '0:45', status: 'connected', sentiment: 'neutral' },
  { id: '3', contact: 'Emily Davis', phone: '+1 (555) 345-6789', agent: 'Sales Bot B', duration: '0:08', status: 'ringing', sentiment: 'neutral' },
  { id: '4', contact: 'James Wilson', phone: '+1 (555) 456-7890', agent: 'Collections Bot', duration: '4:32', status: 'connected', sentiment: 'negative' },
  { id: '5', contact: 'Lisa Anderson', phone: '+1 (555) 567-8901', agent: 'Booking Bot', duration: '1:22', status: 'connected', sentiment: 'positive' },
];

const sentimentColors = {
  positive: 'text-green-600 dark:text-green-400',
  neutral: 'text-gray-500',
  negative: 'text-red-600 dark:text-red-400',
};

const mockTranscript = [
  { speaker: 'agent', text: 'Hello! This is Alex from TechCorp. Am I speaking with Sarah?' },
  { speaker: 'contact', text: 'Yes, this is Sarah.' },
  { speaker: 'agent', text: 'Great! I wanted to follow up on our cloud platform...' },
  { speaker: 'contact', text: 'Oh yes, I remember. I was actually thinking about that.' },
  { speaker: 'agent', text: "That's wonderful to hear! Let me tell you about our latest features..." },
];

export default function MonitorPage() {
  const [selectedCall, setSelectedCall] = useState(mockActiveCalls[0]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-3">
            <h2 className="text-2xl font-bold tracking-tight">Live Monitor</h2>
            <Badge variant="success">
              <span className="mr-1.5 h-1.5 w-1.5 animate-pulse rounded-full bg-green-500" />
              {mockActiveCalls.length} Active Calls
            </Badge>
          </div>
          <p className="text-sm text-muted-foreground">
            Monitor active calls in real-time
          </p>
        </div>
      </div>

      {/* Summary cards */}
      <div className="grid gap-4 sm:grid-cols-4">
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <Activity className="h-5 w-5 text-green-500" />
            <div>
              <p className="text-xs text-muted-foreground">Active Now</p>
              <p className="text-2xl font-bold">5</p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <Phone className="h-5 w-5 text-blue-500" />
            <div>
              <p className="text-xs text-muted-foreground">Calls Today</p>
              <p className="text-2xl font-bold">342</p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <PhoneCall className="h-5 w-5 text-emerald-500" />
            <div>
              <p className="text-xs text-muted-foreground">Connected</p>
              <p className="text-2xl font-bold">4</p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <Volume2 className="h-5 w-5 text-orange-500" />
            <div>
              <p className="text-xs text-muted-foreground">Avg Wait</p>
              <p className="text-2xl font-bold">3s</p>
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        {/* Active calls list */}
        <Card className="lg:col-span-1">
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Active Calls</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 p-3">
            {mockActiveCalls.map((call) => (
              <button
                key={call.id}
                onClick={() => setSelectedCall(call)}
                className={`w-full rounded-lg border p-3 text-left transition-colors ${
                  selectedCall?.id === call.id
                    ? 'border-primary bg-primary/5'
                    : 'hover:bg-muted/50'
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">{call.contact}</span>
                  <Badge variant={call.status === 'connected' ? 'success' : 'warning'} className="text-[10px]">
                    {call.status}
                  </Badge>
                </div>
                <div className="mt-1 flex items-center justify-between text-xs text-muted-foreground">
                  <span>{call.agent}</span>
                  <span className="font-mono">{call.duration}</span>
                </div>
                <div className="mt-1">
                  <span className={`text-[10px] ${sentimentColors[call.sentiment as keyof typeof sentimentColors]}`}>
                    Sentiment: {call.sentiment}
                  </span>
                </div>
              </button>
            ))}
          </CardContent>
        </Card>

        {/* Live transcript */}
        <Card className="lg:col-span-2">
          <CardHeader className="flex flex-row items-center justify-between pb-3">
            <div>
              <CardTitle className="text-base">Live Transcript</CardTitle>
              <p className="text-xs text-muted-foreground">
                {selectedCall?.contact} &middot; {selectedCall?.agent}
              </p>
            </div>
            <div className="flex gap-2">
              <Button variant="outline" size="sm">
                <Eye className="mr-2 h-4 w-4" />
                Listen
              </Button>
              <Button variant="outline" size="sm" className="text-destructive hover:text-destructive">
                <PauseCircle className="mr-2 h-4 w-4" />
                End Call
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            <div className="space-y-4 max-h-[500px] overflow-y-auto scrollbar-thin">
              {mockTranscript.map((entry, i) => (
                <div key={i} className="flex gap-3">
                  <div
                    className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-medium ${
                      entry.speaker === 'agent'
                        ? 'bg-primary/10 text-primary'
                        : 'bg-orange-100 text-orange-600 dark:bg-orange-900/30 dark:text-orange-400'
                    }`}
                  >
                    {entry.speaker === 'agent' ? 'AI' : 'C'}
                  </div>
                  <div>
                    <p className="text-xs font-semibold text-muted-foreground">
                      {entry.speaker === 'agent' ? 'AI Agent' : 'Contact'}
                    </p>
                    <p className="text-sm leading-relaxed">{entry.text}</p>
                  </div>
                </div>
              ))}
              {/* Typing indicator */}
              <div className="flex gap-3">
                <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/10 text-xs font-medium text-primary">
                  AI
                </div>
                <div className="flex items-center gap-1 pt-3">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary [animation-delay:150ms]" />
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary [animation-delay:300ms]" />
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
