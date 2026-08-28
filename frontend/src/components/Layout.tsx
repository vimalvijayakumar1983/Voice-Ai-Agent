import Link from 'next/link';
import { useRouter } from 'next/router';
import { ReactNode, useEffect, useRef, useState } from 'react';
import {
  Activity,
  AudioWaveform,
  Bot,
  ChevronDown,
  CreditCard,
  FlaskConical,
  LogOut,
  Menu,
  Megaphone,
  PhoneCall,
  Plug,
  Settings,
  ShieldCheck,
  Sparkles,
  Workflow,
  X,
} from 'lucide-react';
import { api, CurrentUser } from '@/lib/api';

const navigation = [
  {
    label: 'Workspace',
    items: [
      { href: '/', label: 'Overview', icon: Activity },
      { href: '/agents', label: 'Agents', icon: Bot },
      { href: '/playground', label: 'Playground', icon: FlaskConical },
      { href: '/calls', label: 'Conversations', icon: PhoneCall },
      { href: '/workflows', label: 'Workflows', icon: Workflow },
    ],
  },
  {
    label: 'Operate',
    items: [
      { href: '/campaigns', label: 'Campaigns', icon: Megaphone },
      { href: '/compliance', label: 'Compliance', icon: ShieldCheck },
      { href: '/integrations', label: 'Integrations', icon: Plug },
    ],
  },
  {
    label: 'Manage',
    items: [
      { href: '/billing', label: 'Usage & billing', icon: CreditCard },
      { href: '/settings', label: 'Settings', icon: Settings },
    ],
  },
];

function getInitials(user: CurrentUser | null) {
  const source = user?.full_name?.trim() || user?.email || 'Account';
  return source
    .split(/[\s@._-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join('') || 'AC';
}

function formatRole(role?: string) {
  if (!role) return 'Workspace member';
  return role.replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function getWorkspaceInitials(user: CurrentUser | null) {
  const name = user?.tenant_name?.trim();
  if (!name) return 'VA';
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join('');
}

export default function Layout({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [profileOpen, setProfileOpen] = useState(false);
  const profileRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let active = true;
    api.getMe().then((currentUser) => {
      if (active) setUser(currentUser);
    }).catch(() => {
      // The application-level session guard handles expired sessions.
    });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!profileOpen) return;

    const handlePointerDown = (event: PointerEvent) => {
      if (!profileRef.current?.contains(event.target as Node)) setProfileOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setProfileOpen(false);
    };
    document.addEventListener('pointerdown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [profileOpen]);

  const logout = () => {
    setProfileOpen(false);
    api.logout();
  };

  const isActive = (href: string) =>
    href === '/' ? router.pathname === '/' : router.pathname.startsWith(href);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <aside className={`sidebar ${mobileOpen ? 'sidebar-open' : ''}`}>
        <div className="brand-lockup">
          <div className="brand-mark"><AudioWaveform size={20} strokeWidth={2.4} /></div>
          <div>
            <strong>VAV Voice AI</strong>
            <span>Enterprise workspace</span>
          </div>
          <button className="icon-button mobile-close" onClick={() => setMobileOpen(false)} aria-label="Close menu">
            <X size={18} />
          </button>
        </div>

        <div className="workspace-switcher workspace-static" aria-label="Current workspace environment">
          <div className="workspace-avatar">{getWorkspaceInitials(user)}</div>
          <div><strong>{user?.tenant_name || 'Voice operations'}</strong><span>Production workspace</span></div>
          <ShieldCheck size={15} aria-hidden="true" />
        </div>

        <nav className="sidebar-nav" aria-label="Primary navigation">
          {navigation.map((group) => (
            <div className="nav-group" key={group.label}>
              <span className="nav-label">{group.label}</span>
              {group.items.map((item) => {
                const Icon = item.icon;
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={isActive(item.href) ? 'active' : ''}
                    aria-current={isActive(item.href) ? 'page' : undefined}
                    onClick={() => setMobileOpen(false)}
                  >
                    <Icon size={17} strokeWidth={1.9} />
                    <span>{item.label}</span>
                    {item.href === '/playground' && <span className="nav-new">Live</span>}
                  </Link>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className="provider-pill"><span className="status-dot" /> Smallest.ai native</div>
          <p>Powered by Atoms, Pulse & Lightning</p>
        </div>
      </aside>

      {mobileOpen && <button className="sidebar-backdrop" onClick={() => setMobileOpen(false)} aria-label="Close navigation" />}

      <div className="app-main">
        <header className="topbar">
          <button className="icon-button mobile-menu" onClick={() => setMobileOpen(true)} aria-label="Open menu">
            <Menu size={19} />
          </button>
          <div className="topbar-context">
            <span className="status-dot" aria-hidden="true" />
            <span>Secure production workspace</span>
          </div>
          <div className="topbar-actions">
            <Link className="ai-action" href="/agents"><Sparkles size={15} /> Create with AI</Link>
            <div className="profile-control" ref={profileRef}>
              <button
                type="button"
                className="profile-menu"
                aria-expanded={profileOpen}
                aria-haspopup="menu"
                onClick={() => setProfileOpen((open) => !open)}
              >
                <span>{getInitials(user)}</span>
                <div><strong>{user?.full_name || 'Your account'}</strong><small>{formatRole(user?.role)}</small></div>
                <ChevronDown size={14} aria-hidden="true" />
              </button>
              {profileOpen ? (
                <div className="profile-popover" role="menu">
                  <div className="profile-popover-identity">
                    <strong>{user?.full_name || 'Signed-in user'}</strong>
                    <span>{user?.email || 'Loading account…'}</span>
                  </div>
                  <button type="button" role="menuitem" onClick={logout}>
                    <LogOut size={15} /> Sign out
                  </button>
                </div>
              ) : null}
            </div>
          </div>
        </header>
        <main id="main-content" className="main-content" tabIndex={-1}>{children}</main>
      </div>
    </div>
  );
}
