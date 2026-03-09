'use client';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Badge } from '@/components/ui/badge';
import { Save, ShieldCheck, AlertTriangle } from 'lucide-react';

export default function CompliancePage() {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">Compliance Settings</h2>
        <p className="text-sm text-muted-foreground">
          Configure regulatory compliance and data policies
        </p>
      </div>

      {/* Compliance Status */}
      <div className="grid gap-4 sm:grid-cols-3">
        {[
          { label: 'TCPA', status: 'Compliant', icon: ShieldCheck, color: 'text-green-600' },
          { label: 'GDPR', status: 'Compliant', icon: ShieldCheck, color: 'text-green-600' },
          { label: 'HIPAA', status: 'Not Configured', icon: AlertTriangle, color: 'text-yellow-600' },
        ].map((item) => (
          <Card key={item.label}>
            <CardContent className="flex items-center gap-3 p-4">
              <item.icon className={`h-5 w-5 ${item.color}`} />
              <div>
                <p className="text-sm font-medium">{item.label}</p>
                <p className="text-xs text-muted-foreground">{item.status}</p>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Data Retention</CardTitle>
          <CardDescription>Configure how long data is stored.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label>Call Recordings</Label>
              <Select defaultValue="90">
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="30">30 days</SelectItem>
                  <SelectItem value="90">90 days</SelectItem>
                  <SelectItem value="180">180 days</SelectItem>
                  <SelectItem value="365">1 year</SelectItem>
                  <SelectItem value="0">Forever</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>Call Transcripts</Label>
              <Select defaultValue="365">
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="90">90 days</SelectItem>
                  <SelectItem value="180">180 days</SelectItem>
                  <SelectItem value="365">1 year</SelectItem>
                  <SelectItem value="0">Forever</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="space-y-2">
            <Label>Contact Data</Label>
            <Select defaultValue="0">
              <SelectTrigger className="w-[200px]"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="365">1 year</SelectItem>
                <SelectItem value="730">2 years</SelectItem>
                <SelectItem value="0">Until deleted</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Consent Configuration</CardTitle>
          <CardDescription>Set up required consent and disclosure scripts.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label>Recording Disclosure Script</Label>
            <Textarea
              defaultValue="This call may be recorded for quality assurance and training purposes."
              className="min-h-[80px]"
            />
          </div>
          <div className="space-y-2">
            <Label>AI Agent Disclosure</Label>
            <Textarea
              defaultValue="I should let you know that I am an AI assistant calling on behalf of [Company]."
              className="min-h-[80px]"
            />
          </div>
          <div className="space-y-2">
            <Label>DNC (Do Not Call) Policy</Label>
            <Select defaultValue="immediate">
              <SelectTrigger className="w-[250px]"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="immediate">Immediately honor requests</SelectItem>
                <SelectItem value="24h">Process within 24 hours</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex justify-end">
            <Button>
              <Save className="mr-2 h-4 w-4" />
              Save Compliance Settings
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
