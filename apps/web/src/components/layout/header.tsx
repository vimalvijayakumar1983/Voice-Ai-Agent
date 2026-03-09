'use client';

import { usePathname } from 'next/navigation';
import { Bell, Search } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { UserMenu } from './user-menu';
import { Badge } from '@/components/ui/badge';

const pathTitles: Record<string, string> = {
  '/dashboard': 'Dashboard',
  '/agents': 'AI Agents',
  '/agents/new': 'Create Agent',
  '/workflows': 'Workflows',
  '/workflows/new': 'Create Workflow',
  '/contacts': 'Contacts',
  '/calls': 'Call Log',
  '/campaigns': 'Campaigns',
  '/campaigns/new': 'Create Campaign',
  '/analytics': 'Analytics',
  '/monitor': 'Live Monitor',
  '/settings': 'Settings',
  '/settings/general': 'General Settings',
  '/settings/team': 'Team Management',
  '/settings/integrations': 'Integrations',
  '/settings/billing': 'Billing & Usage',
  '/settings/phone-numbers': 'Phone Numbers',
  '/settings/compliance': 'Compliance',
  '/admin': 'Admin Panel',
  '/admin/tenants': 'Tenant Management',
};

function getPageTitle(pathname: string): string {
  if (pathTitles[pathname]) return pathTitles[pathname];
  // Check for dynamic routes
  if (pathname.match(/\/agents\/.+/)) return 'Agent Details';
  if (pathname.match(/\/workflows\/.+/)) return 'Workflow Details';
  if (pathname.match(/\/contacts\/.+/)) return 'Contact Details';
  if (pathname.match(/\/calls\/.+/)) return 'Call Details';
  if (pathname.match(/\/campaigns\/.+/)) return 'Campaign Details';
  return 'VoiceAI';
}

function getBreadcrumbs(pathname: string): { label: string; href?: string }[] {
  const segments = pathname.split('/').filter(Boolean);
  const crumbs: { label: string; href?: string }[] = [];
  let path = '';
  for (const seg of segments) {
    path += `/${seg}`;
    const title = pathTitles[path] || seg.charAt(0).toUpperCase() + seg.slice(1);
    crumbs.push({ label: title, href: path });
  }
  return crumbs;
}

export function Header() {
  const pathname = usePathname();
  const title = getPageTitle(pathname);
  const breadcrumbs = getBreadcrumbs(pathname);

  return (
    <header className="flex h-16 items-center justify-between border-b bg-card px-6">
      <div className="flex flex-col">
        <h1 className="text-lg font-semibold">{title}</h1>
        {breadcrumbs.length > 1 && (
          <nav className="flex items-center gap-1 text-xs text-muted-foreground">
            {breadcrumbs.map((crumb, i) => (
              <span key={i} className="flex items-center gap-1">
                {i > 0 && <span>/</span>}
                <span className={i === breadcrumbs.length - 1 ? 'text-foreground' : ''}>
                  {crumb.label}
                </span>
              </span>
            ))}
          </nav>
        )}
      </div>

      <div className="flex items-center gap-4">
        {/* Search */}
        <div className="relative hidden md:block">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search anything..."
            className="w-64 bg-muted/50 pl-9"
          />
        </div>

        {/* Notifications */}
        <Button variant="ghost" size="icon" className="relative">
          <Bell className="h-4.5 w-4.5" />
          <Badge className="absolute -right-0.5 -top-0.5 flex h-4 w-4 items-center justify-center p-0 text-[10px]">
            3
          </Badge>
        </Button>

        {/* User Menu */}
        <UserMenu />
      </div>
    </header>
  );
}
