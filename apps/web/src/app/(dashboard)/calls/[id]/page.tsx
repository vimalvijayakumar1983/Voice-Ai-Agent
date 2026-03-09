'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { TranscriptViewer } from '@/components/calls/transcript-viewer';
import { CallPlayer } from '@/components/calls/call-player';
import { CallStatusBadge } from '@/components/calls/call-status-badge';
import { PhoneOutgoing, Clock, Bot, User, CheckCircle, AlertCircle } from 'lucide-react';

const mockTranscript = [
  { id: '1', speaker: 'agent' as const, text: 'Hello! This is Alex from TechCorp Solutions. Am I speaking with Sarah Johnson?', timestamp: '0:00', sentiment: 'neutral' as const },
  { id: '2', speaker: 'contact' as const, text: 'Yes, this is Sarah. How can I help you?', timestamp: '0:05', sentiment: 'neutral' as const },
  { id: '3', speaker: 'agent' as const, text: "Great to connect with you, Sarah. I'm reaching out because we've helped companies like yours streamline their cloud infrastructure. Do you have a moment to chat?", timestamp: '0:08', sentiment: 'positive' as const },
  { id: '4', speaker: 'contact' as const, text: 'Sure, I have a few minutes. What exactly do you offer?', timestamp: '0:18', sentiment: 'positive' as const },
  { id: '5', speaker: 'agent' as const, text: 'We provide an all-in-one cloud platform that can reduce your infrastructure costs by up to 40%. Many of our clients in the technology sector have seen significant improvements in deployment speed and reliability.', timestamp: '0:22', sentiment: 'positive' as const },
  { id: '6', speaker: 'contact' as const, text: "That sounds interesting. We've been looking at optimizing our cloud spend actually.", timestamp: '0:38', sentiment: 'positive' as const },
  { id: '7', speaker: 'agent' as const, text: "Perfect timing! I'd love to schedule a quick 15-minute demo with our solutions architect. Would Thursday or Friday work better for you?", timestamp: '0:45', sentiment: 'positive' as const },
  { id: '8', speaker: 'contact' as const, text: "Thursday afternoon would work. Let's say 2 PM?", timestamp: '0:52', sentiment: 'positive' as const },
  { id: '9', speaker: 'agent' as const, text: "Excellent! I'll send you a calendar invite for Thursday at 2 PM. Thank you for your time, Sarah. Looking forward to it!", timestamp: '0:56', sentiment: 'positive' as const },
];

export default function CallDetailPage({ params }: { params: { id: string } }) {
  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-3">
            <h2 className="text-2xl font-bold tracking-tight">Call Details</h2>
            <CallStatusBadge status="completed" />
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            Call ID: call_{params.id} &middot; March 7, 2024 at 2:15 PM
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm">Add to Campaign</Button>
          <Button variant="outline" size="sm">Flag for Review</Button>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        {/* Main content */}
        <div className="lg:col-span-2 space-y-6">
          {/* Player */}
          <CallPlayer duration="3:42" />

          {/* Transcript */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Conversation Transcript</CardTitle>
            </CardHeader>
            <CardContent>
              <TranscriptViewer entries={mockTranscript} />
            </CardContent>
          </Card>
        </div>

        {/* Sidebar */}
        <div className="space-y-4">
          {/* Summary */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Call Summary</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="text-sm leading-relaxed text-muted-foreground">
                Successful outbound sales call with Sarah Johnson from TechCorp.
                Contact expressed interest in cloud infrastructure optimization.
                Demo scheduled for Thursday at 2 PM with solutions architect.
              </p>
              <Separator />
              <div className="space-y-3">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted-foreground">Outcome</span>
                  <Badge variant="success">Interested - Demo Booked</Badge>
                </div>
                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted-foreground">Sentiment</span>
                  <Badge variant="success">Positive</Badge>
                </div>
                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted-foreground">Direction</span>
                  <div className="flex items-center gap-1.5">
                    <PhoneOutgoing className="h-3.5 w-3.5 text-blue-500" />
                    <span>Outbound</span>
                  </div>
                </div>
                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted-foreground">Duration</span>
                  <span className="font-mono">3:42</span>
                </div>
              </div>
            </CardContent>
          </Card>

          {/* Metadata */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Metadata</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {[
                { icon: User, label: 'Contact', value: 'Sarah Johnson' },
                { icon: Bot, label: 'Agent', value: 'Sales Outreach Pro' },
                { icon: Clock, label: 'Started', value: '2:15:00 PM' },
                { icon: Clock, label: 'Ended', value: '2:18:42 PM' },
              ].map((item) => (
                <div key={item.label} className="flex items-center gap-3 text-sm">
                  <item.icon className="h-4 w-4 text-muted-foreground" />
                  <span className="text-muted-foreground">{item.label}:</span>
                  <span className="font-medium">{item.value}</span>
                </div>
              ))}
            </CardContent>
          </Card>

          {/* Compliance */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Compliance Checklist</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {[
                { label: 'Consent obtained', checked: true },
                { label: 'Recording disclosed', checked: true },
                { label: 'Company identified', checked: true },
                { label: 'DNC check passed', checked: true },
                { label: 'No prohibited phrases', checked: true },
              ].map((item) => (
                <div key={item.label} className="flex items-center gap-2 text-sm">
                  {item.checked ? (
                    <CheckCircle className="h-4 w-4 text-green-500" />
                  ) : (
                    <AlertCircle className="h-4 w-4 text-red-500" />
                  )}
                  <span>{item.label}</span>
                </div>
              ))}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
