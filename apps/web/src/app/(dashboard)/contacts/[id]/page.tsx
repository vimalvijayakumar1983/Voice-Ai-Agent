'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Separator } from '@/components/ui/separator';
import { Textarea } from '@/components/ui/textarea';
import {
  Phone,
  Mail,
  Building,
  MapPin,
  Calendar,
  Edit,
  PhoneCall,
  MessageSquare,
} from 'lucide-react';

export default function ContactDetailPage({ params }: { params: { id: string } }) {
  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-4">
          <div className="flex h-14 w-14 items-center justify-center rounded-full bg-primary/10 text-xl font-bold text-primary">
            SJ
          </div>
          <div>
            <h2 className="text-2xl font-bold tracking-tight">Sarah Johnson</h2>
            <p className="text-sm text-muted-foreground">TechCorp &middot; VP of Sales</p>
            <div className="mt-1 flex gap-2">
              <Badge variant="success">Active</Badge>
              <Badge variant="outline">hot-lead</Badge>
              <Badge variant="outline">enterprise</Badge>
            </div>
          </div>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm">
            <Edit className="mr-2 h-4 w-4" />
            Edit
          </Button>
          <Button size="sm">
            <PhoneCall className="mr-2 h-4 w-4" />
            Call Now
          </Button>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        {/* Contact info sidebar */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Contact Information</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {[
              { icon: Phone, label: 'Phone', value: '+1 (555) 123-4567' },
              { icon: Mail, label: 'Email', value: 'sarah@techcorp.com' },
              { icon: Building, label: 'Company', value: 'TechCorp' },
              { icon: MapPin, label: 'Location', value: 'San Francisco, CA' },
              { icon: Calendar, label: 'Added', value: 'Jan 10, 2024' },
            ].map((item) => (
              <div key={item.label} className="flex items-center gap-3">
                <item.icon className="h-4 w-4 text-muted-foreground" />
                <div>
                  <p className="text-xs text-muted-foreground">{item.label}</p>
                  <p className="text-sm">{item.value}</p>
                </div>
              </div>
            ))}
            <Separator />
            <div>
              <p className="mb-2 text-xs font-medium text-muted-foreground">NOTES</p>
              <Textarea placeholder="Add notes about this contact..." className="min-h-[100px]" />
              <Button size="sm" className="mt-2">
                Save Note
              </Button>
            </div>
          </CardContent>
        </Card>

        {/* Main content */}
        <div className="lg:col-span-2">
          <Tabs defaultValue="history">
            <TabsList>
              <TabsTrigger value="history">Interaction History</TabsTrigger>
              <TabsTrigger value="calls">Call History</TabsTrigger>
              <TabsTrigger value="notes">Notes</TabsTrigger>
            </TabsList>

            <TabsContent value="history" className="space-y-3">
              {[
                { type: 'call', text: 'Outbound call - Interested in demo', time: '2 hours ago', agent: 'Sales Bot A' },
                { type: 'call', text: 'Inbound call - Pricing question', time: '2 days ago', agent: 'Support Bot' },
                { type: 'note', text: 'Requested follow-up after board meeting', time: '3 days ago', agent: 'John Smith' },
                { type: 'call', text: 'Outbound call - Initial outreach', time: '1 week ago', agent: 'Sales Bot A' },
              ].map((item, i) => (
                <Card key={i}>
                  <CardContent className="flex items-start gap-3 p-4">
                    <div className={`mt-0.5 rounded-full p-2 ${item.type === 'call' ? 'bg-blue-100 dark:bg-blue-900/30' : 'bg-orange-100 dark:bg-orange-900/30'}`}>
                      {item.type === 'call' ? (
                        <Phone className="h-3.5 w-3.5 text-blue-600 dark:text-blue-400" />
                      ) : (
                        <MessageSquare className="h-3.5 w-3.5 text-orange-600 dark:text-orange-400" />
                      )}
                    </div>
                    <div className="flex-1">
                      <p className="text-sm">{item.text}</p>
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        {item.agent} &middot; {item.time}
                      </p>
                    </div>
                  </CardContent>
                </Card>
              ))}
            </TabsContent>

            <TabsContent value="calls">
              <Card>
                <CardContent className="p-4">
                  <p className="text-sm text-muted-foreground">Detailed call history with recordings and transcripts.</p>
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="notes">
              <Card>
                <CardContent className="p-4">
                  <p className="text-sm text-muted-foreground">All notes for this contact.</p>
                </CardContent>
              </Card>
            </TabsContent>
          </Tabs>
        </div>
      </div>
    </div>
  );
}
