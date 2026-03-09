'use client';

import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Save, Play } from 'lucide-react';

const campaignSchema = z.object({
  name: z.string().min(1, 'Campaign name is required'),
  description: z.string().optional(),
  agentId: z.string().min(1, 'Please select an agent'),
  contactListId: z.string().min(1, 'Please select a contact list'),
  scheduleType: z.string().min(1, 'Schedule type is required'),
  startDate: z.string().optional(),
  endDate: z.string().optional(),
  callsPerMinute: z.number().min(1).max(100),
  maxConcurrent: z.number().min(1).max(50),
  timezone: z.string(),
  startTime: z.string(),
  endTime: z.string(),
  retryAttempts: z.number().min(0).max(5),
});

type CampaignFormData = z.infer<typeof campaignSchema>;

export default function NewCampaignPage() {
  const form = useForm<CampaignFormData>({
    resolver: zodResolver(campaignSchema),
    defaultValues: {
      name: '',
      description: '',
      agentId: '',
      contactListId: '',
      scheduleType: 'immediate',
      callsPerMinute: 10,
      maxConcurrent: 5,
      timezone: 'America/New_York',
      startTime: '09:00',
      endTime: '17:00',
      retryAttempts: 2,
    },
  });

  const onSubmit = (data: CampaignFormData) => {
    console.log('Campaign created:', data);
  };

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">Create Campaign</h2>
        <p className="text-sm text-muted-foreground">
          Set up a new outbound calling campaign
        </p>
      </div>

      <Form {...form}>
        <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Campaign Details</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <FormField control={form.control} name="name" render={({ field }) => (
                <FormItem>
                  <FormLabel>Campaign Name</FormLabel>
                  <FormControl><Input placeholder="Q1 Sales Outreach" {...field} /></FormControl>
                  <FormMessage />
                </FormItem>
              )} />
              <FormField control={form.control} name="description" render={({ field }) => (
                <FormItem>
                  <FormLabel>Description</FormLabel>
                  <FormControl><Textarea placeholder="Campaign goals and notes..." {...field} /></FormControl>
                  <FormMessage />
                </FormItem>
              )} />
              <div className="grid gap-4 sm:grid-cols-2">
                <FormField control={form.control} name="agentId" render={({ field }) => (
                  <FormItem>
                    <FormLabel>AI Agent</FormLabel>
                    <Select onValueChange={field.onChange} defaultValue={field.value}>
                      <FormControl><SelectTrigger><SelectValue placeholder="Select agent" /></SelectTrigger></FormControl>
                      <SelectContent>
                        <SelectItem value="1">Sales Outreach Pro</SelectItem>
                        <SelectItem value="2">Customer Support Bot</SelectItem>
                        <SelectItem value="3">Appointment Scheduler</SelectItem>
                      </SelectContent>
                    </Select>
                    <FormMessage />
                  </FormItem>
                )} />
                <FormField control={form.control} name="contactListId" render={({ field }) => (
                  <FormItem>
                    <FormLabel>Contact List</FormLabel>
                    <Select onValueChange={field.onChange} defaultValue={field.value}>
                      <FormControl><SelectTrigger><SelectValue placeholder="Select list" /></SelectTrigger></FormControl>
                      <SelectContent>
                        <SelectItem value="1">All Leads (5,420)</SelectItem>
                        <SelectItem value="2">Hot Leads (890)</SelectItem>
                        <SelectItem value="3">Enterprise (340)</SelectItem>
                      </SelectContent>
                    </Select>
                    <FormMessage />
                  </FormItem>
                )} />
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Schedule & Pacing</CardTitle>
              <CardDescription>Configure when and how fast calls are made.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <FormField control={form.control} name="startTime" render={({ field }) => (
                  <FormItem>
                    <FormLabel>Start Time</FormLabel>
                    <FormControl><Input type="time" {...field} /></FormControl>
                    <FormMessage />
                  </FormItem>
                )} />
                <FormField control={form.control} name="endTime" render={({ field }) => (
                  <FormItem>
                    <FormLabel>End Time</FormLabel>
                    <FormControl><Input type="time" {...field} /></FormControl>
                    <FormMessage />
                  </FormItem>
                )} />
              </div>
              <div className="grid gap-4 sm:grid-cols-3">
                <FormField control={form.control} name="callsPerMinute" render={({ field }) => (
                  <FormItem>
                    <FormLabel>Calls/Minute</FormLabel>
                    <FormControl><Input type="number" {...field} onChange={(e) => field.onChange(parseInt(e.target.value))} /></FormControl>
                    <FormMessage />
                  </FormItem>
                )} />
                <FormField control={form.control} name="maxConcurrent" render={({ field }) => (
                  <FormItem>
                    <FormLabel>Max Concurrent</FormLabel>
                    <FormControl><Input type="number" {...field} onChange={(e) => field.onChange(parseInt(e.target.value))} /></FormControl>
                    <FormMessage />
                  </FormItem>
                )} />
                <FormField control={form.control} name="retryAttempts" render={({ field }) => (
                  <FormItem>
                    <FormLabel>Retry Attempts</FormLabel>
                    <FormControl><Input type="number" {...field} onChange={(e) => field.onChange(parseInt(e.target.value))} /></FormControl>
                    <FormMessage />
                  </FormItem>
                )} />
              </div>
            </CardContent>
          </Card>

          <div className="flex items-center justify-end gap-3">
            <Button type="submit" variant="outline">
              <Save className="mr-2 h-4 w-4" />
              Save as Draft
            </Button>
            <Button type="submit">
              <Play className="mr-2 h-4 w-4" />
              Create & Start
            </Button>
          </div>
        </form>
      </Form>
    </div>
  );
}
