'use client';

import { useState, useCallback } from 'react';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Link2,
  Unlink,
  CheckCircle,
  AlertCircle,
  Loader2,
  ExternalLink,
  RefreshCw,
} from 'lucide-react';
import { cn } from '@/lib/utils';

// ---------- Types ----------

type CRMProvider = 'salesforce' | 'hubspot';

interface CRMConnectionStatus {
  connected: boolean;
  provider?: CRMProvider;
  lastSyncAt?: string;
  syncStatus?: 'idle' | 'syncing' | 'success' | 'failed';
  message?: string;
}

interface CRMConnectProps {
  status: CRMConnectionStatus;
  onConnect: (provider: CRMProvider) => Promise<{ authorizationUrl: string }>;
  onDisconnect: () => Promise<void>;
  onSync: () => Promise<void>;
  isLoading?: boolean;
  className?: string;
}

interface ProviderInfo {
  id: CRMProvider;
  name: string;
  description: string;
  logo: string;
  color: string;
  bgColor: string;
}

const PROVIDERS: ProviderInfo[] = [
  {
    id: 'salesforce',
    name: 'Salesforce',
    description: 'Connect your Salesforce CRM to sync contacts, deals, and log call activities.',
    logo: 'SF',
    color: 'text-blue-600',
    bgColor: 'bg-blue-100 dark:bg-blue-900/30',
  },
  {
    id: 'hubspot',
    name: 'HubSpot',
    description: 'Integrate with HubSpot CRM for contact sync, deal tracking, and engagement logging.',
    logo: 'HS',
    color: 'text-orange-600',
    bgColor: 'bg-orange-100 dark:bg-orange-900/30',
  },
];

// ---------- Component ----------

export function CRMConnect({
  status,
  onConnect,
  onDisconnect,
  onSync,
  isLoading = false,
  className,
}: CRMConnectProps) {
  const [connectingProvider, setConnectingProvider] = useState<CRMProvider | null>(null);
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  const [isSyncing, setIsSyncing] = useState(false);

  const handleConnect = useCallback(
    async (provider: CRMProvider) => {
      setConnectingProvider(provider);
      try {
        const result = await onConnect(provider);
        // Redirect to OAuth authorization URL
        window.location.href = result.authorizationUrl;
      } catch (error) {
        console.error('Connect failed:', error);
        setConnectingProvider(null);
      }
    },
    [onConnect],
  );

  const handleDisconnect = useCallback(async () => {
    if (!confirm('Are you sure you want to disconnect? This will stop all syncing.')) {
      return;
    }

    setIsDisconnecting(true);
    try {
      await onDisconnect();
    } catch (error) {
      console.error('Disconnect failed:', error);
    } finally {
      setIsDisconnecting(false);
    }
  }, [onDisconnect]);

  const handleSync = useCallback(async () => {
    setIsSyncing(true);
    try {
      await onSync();
    } catch (error) {
      console.error('Sync failed:', error);
    } finally {
      setIsSyncing(false);
    }
  }, [onSync]);

  if (status.connected && status.provider) {
    const provider = PROVIDERS.find((p) => p.id === status.provider);

    return (
      <Card className={className}>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div
                className={cn(
                  'h-10 w-10 rounded-lg flex items-center justify-center font-bold text-sm',
                  provider?.bgColor,
                  provider?.color,
                )}
              >
                {provider?.logo}
              </div>
              <div>
                <CardTitle className="text-lg">{provider?.name} CRM</CardTitle>
                <CardDescription>Connected and syncing</CardDescription>
              </div>
            </div>
            <Badge variant="success" className="flex items-center gap-1">
              <CheckCircle className="h-3 w-3" />
              Connected
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Sync Status */}
          <div className="rounded-lg border p-4 space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium">Sync Status</span>
              <SyncStatusBadge status={status.syncStatus || 'idle'} />
            </div>

            {status.lastSyncAt && (
              <p className="text-xs text-muted-foreground">
                Last synced: {new Date(status.lastSyncAt).toLocaleString()}
              </p>
            )}

            {status.message && (
              <p className="text-xs text-muted-foreground">{status.message}</p>
            )}
          </div>

          {/* Actions */}
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={handleSync}
              disabled={isSyncing || status.syncStatus === 'syncing'}
            >
              {isSyncing || status.syncStatus === 'syncing' ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <RefreshCw className="mr-2 h-4 w-4" />
              )}
              Sync Now
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="text-destructive hover:text-destructive"
              onClick={handleDisconnect}
              disabled={isDisconnecting}
            >
              {isDisconnecting ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <Unlink className="mr-2 h-4 w-4" />
              )}
              Disconnect
            </Button>
          </div>
        </CardContent>
      </Card>
    );
  }

  // Not connected - show provider selection
  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle className="text-lg flex items-center gap-2">
          <Link2 className="h-5 w-5" />
          Connect CRM
        </CardTitle>
        <CardDescription>
          Connect your CRM to sync contacts, log call activities, and track deals automatically.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {PROVIDERS.map((provider) => (
          <div
            key={provider.id}
            className="flex items-center justify-between rounded-lg border p-4 hover:border-primary/30 transition-colors"
          >
            <div className="flex items-center gap-3">
              <div
                className={cn(
                  'h-10 w-10 rounded-lg flex items-center justify-center font-bold text-sm',
                  provider.bgColor,
                  provider.color,
                )}
              >
                {provider.logo}
              </div>
              <div>
                <h4 className="text-sm font-medium">{provider.name}</h4>
                <p className="text-xs text-muted-foreground max-w-sm">
                  {provider.description}
                </p>
              </div>
            </div>
            <Button
              size="sm"
              onClick={() => handleConnect(provider.id)}
              disabled={connectingProvider !== null || isLoading}
            >
              {connectingProvider === provider.id ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Connecting...
                </>
              ) : (
                <>
                  <ExternalLink className="mr-2 h-4 w-4" />
                  Connect
                </>
              )}
            </Button>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

// ---------- Sub-components ----------

function SyncStatusBadge({ status }: { status: string }) {
  switch (status) {
    case 'syncing':
      return (
        <Badge variant="default" className="flex items-center gap-1">
          <Loader2 className="h-3 w-3 animate-spin" />
          Syncing
        </Badge>
      );
    case 'success':
      return (
        <Badge variant="success" className="flex items-center gap-1">
          <CheckCircle className="h-3 w-3" />
          Success
        </Badge>
      );
    case 'failed':
      return (
        <Badge variant="destructive" className="flex items-center gap-1">
          <AlertCircle className="h-3 w-3" />
          Failed
        </Badge>
      );
    default:
      return <Badge variant="secondary">Idle</Badge>;
  }
}
