'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import {
  LayoutDashboard,
  Bot,
  GitBranch,
  Users,
  Phone,
  Megaphone,
  BarChart3,
  Activity,
  Settings,
  Shield,
  ChevronLeft,
  ChevronRight,
  Zap,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useAppStore } from '@/stores/app-store';
import { useAuthStore } from '@/stores/auth-store';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { getInitials } from '@/lib/utils';

const mainNavItems = [
  { title: 'Dashboard', href: '/dashboard', icon: LayoutDashboard },
  { title: 'Agents', href: '/agents', icon: Bot },
  { title: 'Workflows', href: '/workflows', icon: GitBranch },
  { title: 'Contacts', href: '/contacts', icon: Users },
  { title: 'Calls', href: '/calls', icon: Phone },
  { title: 'Campaigns', href: '/campaigns', icon: Megaphone },
  { title: 'Analytics', href: '/analytics', icon: BarChart3 },
  { title: 'Live Monitor', href: '/monitor', icon: Activity },
];

const bottomNavItems = [
  { title: 'Settings', href: '/settings', icon: Settings },
];

const adminNavItems = [
  { title: 'Admin', href: '/admin', icon: Shield },
];

export function Sidebar() {
  const pathname = usePathname();
  const { sidebarCollapsed, toggleSidebar } = useAppStore();
  const { user } = useAuthStore();
  const isAdmin = user?.role === 'super_admin';

  return (
    <aside
      className={cn(
        'relative flex h-screen flex-col border-r bg-card transition-all duration-300',
        sidebarCollapsed ? 'w-[68px]' : 'w-[260px]'
      )}
    >
      {/* Logo */}
      <div className="flex h-16 items-center gap-3 border-b px-4">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary">
          <Zap className="h-5 w-5 text-primary-foreground" />
        </div>
        {!sidebarCollapsed && (
          <div className="flex flex-col">
            <span className="text-sm font-bold tracking-tight">VoiceAI</span>
            <span className="text-[10px] text-muted-foreground">Enterprise Platform</span>
          </div>
        )}
      </div>

      {/* Tenant name */}
      {!sidebarCollapsed && user?.tenantName && (
        <div className="border-b px-4 py-3">
          <p className="text-xs font-medium text-muted-foreground">WORKSPACE</p>
          <p className="mt-0.5 truncate text-sm font-semibold">{user.tenantName}</p>
        </div>
      )}

      {/* Navigation */}
      <nav className="flex-1 space-y-1 overflow-y-auto p-3 scrollbar-thin">
        {!sidebarCollapsed && (
          <p className="mb-2 px-3 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            Main
          </p>
        )}
        {mainNavItems.map((item) => {
          const isActive = pathname === item.href || pathname.startsWith(item.href + '/');
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                'flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                isActive
                  ? 'bg-primary/10 text-primary'
                  : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                sidebarCollapsed && 'justify-center px-2'
              )}
              title={sidebarCollapsed ? item.title : undefined}
            >
              <item.icon className={cn('h-4.5 w-4.5 shrink-0', isActive && 'text-primary')} />
              {!sidebarCollapsed && <span>{item.title}</span>}
            </Link>
          );
        })}

        {isAdmin && (
          <>
            <Separator className="my-3" />
            {!sidebarCollapsed && (
              <p className="mb-2 px-3 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                Administration
              </p>
            )}
            {adminNavItems.map((item) => {
              const isActive = pathname === item.href || pathname.startsWith(item.href + '/');
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={cn(
                    'flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                    isActive
                      ? 'bg-primary/10 text-primary'
                      : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                    sidebarCollapsed && 'justify-center px-2'
                  )}
                  title={sidebarCollapsed ? item.title : undefined}
                >
                  <item.icon className="h-4.5 w-4.5 shrink-0" />
                  {!sidebarCollapsed && <span>{item.title}</span>}
                </Link>
              );
            })}
          </>
        )}
      </nav>

      {/* Bottom section */}
      <div className="border-t p-3">
        {bottomNavItems.map((item) => {
          const isActive = pathname === item.href || pathname.startsWith(item.href + '/');
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                'flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                isActive
                  ? 'bg-primary/10 text-primary'
                  : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                sidebarCollapsed && 'justify-center px-2'
              )}
              title={sidebarCollapsed ? item.title : undefined}
            >
              <item.icon className="h-4.5 w-4.5 shrink-0" />
              {!sidebarCollapsed && <span>{item.title}</span>}
            </Link>
          );
        })}

        <Separator className="my-3" />

        {/* User */}
        <div
          className={cn(
            'flex items-center gap-3 rounded-lg px-3 py-2',
            sidebarCollapsed && 'justify-center px-2'
          )}
        >
          <Avatar className="h-8 w-8">
            <AvatarFallback className="bg-primary/10 text-xs text-primary">
              {user?.name ? getInitials(user.name) : 'U'}
            </AvatarFallback>
          </Avatar>
          {!sidebarCollapsed && (
            <div className="flex-1 overflow-hidden">
              <p className="truncate text-sm font-medium">{user?.name || 'User'}</p>
              <p className="truncate text-[11px] text-muted-foreground">{user?.email || ''}</p>
            </div>
          )}
        </div>
      </div>

      {/* Collapse toggle */}
      <Button
        variant="ghost"
        size="icon"
        className="absolute -right-3 top-20 h-6 w-6 rounded-full border bg-background shadow-sm"
        onClick={toggleSidebar}
      >
        {sidebarCollapsed ? (
          <ChevronRight className="h-3 w-3" />
        ) : (
          <ChevronLeft className="h-3 w-3" />
        )}
      </Button>
    </aside>
  );
}
