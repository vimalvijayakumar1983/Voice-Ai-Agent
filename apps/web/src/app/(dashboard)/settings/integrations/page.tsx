'use client';

import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Settings, ExternalLink } from 'lucide-react';

const integrations = [
  { name: 'Salesforce', description: 'Sync contacts, leads, and call logs with Salesforce CRM', icon: '☁️', connected: true, category: 'CRM' },
  { name: 'HubSpot', description: 'Bi-directional sync with HubSpot contacts and deals', icon: '🔶', connected: false, category: 'CRM' },
  { name: 'Twilio', description: 'Phone number provisioning and SIP trunking', icon: '📱', connected: true, category: 'Telephony' },
  { name: 'Slack', description: 'Real-time notifications and call alerts in Slack channels', icon: '💬', connected: true, category: 'Notifications' },
  { name: 'Zapier', description: 'Connect with 5000+ apps via Zapier workflows', icon: '⚡', connected: false, category: 'Automation' },
  { name: 'Webhooks', description: 'Send real-time events to your custom endpoints', icon: '🔗', connected: true, category: 'Developer' },
  { name: 'Google Calendar', description: 'Sync booked appointments with Google Calendar', icon: '📅', connected: false, category: 'Scheduling' },
  { name: 'Stripe', description: 'Process payments and track billing', icon: '💳', connected: true, category: 'Payments' },
];

export default function IntegrationsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">Integrations</h2>
        <p className="text-sm text-muted-foreground">
          Connect your tools and extend VoiceAI capabilities
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        {integrations.map((integration) => (
          <Card key={integration.name} className="transition-shadow hover:shadow-md">
            <CardContent className="p-5">
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                  <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-muted text-xl">
                    {integration.icon}
                  </div>
                  <div>
                    <div className="flex items-center gap-2">
                      <h3 className="font-semibold">{integration.name}</h3>
                      {integration.connected && (
                        <Badge variant="success" className="text-[10px]">Connected</Badge>
                      )}
                    </div>
                    <p className="mt-0.5 text-xs text-muted-foreground">{integration.category}</p>
                  </div>
                </div>
              </div>
              <p className="mt-3 text-sm text-muted-foreground">{integration.description}</p>
              <div className="mt-4">
                {integration.connected ? (
                  <Button variant="outline" size="sm">
                    <Settings className="mr-2 h-4 w-4" />
                    Configure
                  </Button>
                ) : (
                  <Button size="sm">
                    <ExternalLink className="mr-2 h-4 w-4" />
                    Connect
                  </Button>
                )}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
