import Link from 'next/link';
import { useRouter } from 'next/router';
import { ReactNode, useState } from 'react';
import {
  Activity,
  AudioWaveform,
  Bell,
  Bot,
  ChevronDown,
  CreditCard,
  FlaskConical,
  Menu,
  Megaphone,
  PhoneCall,
  Plug,
  Search,
  Settings,
  ShieldCheck,
  Sparkles,
  Workflow,
  X,
} from 'lucide-react';

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

export default function Layout({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [mobileOpen, setMobileOpen] = useState(false);

  const isActive = (href: string) =>
    href === '/' ? router.pathname === '/' : router.pathname.startsWith(href);

  return (
    <div className="app-shell">
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

        <div className="workspace-switcher">
          <div className="workspace-avatar">AZ</div>
          <div><strong>Al Zaabi Group</strong><span>Production</span></div>
          <ChevronDown size={16} />
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
          <div className="command-search">
            <Search size={16} />
            <span>Search agents, calls, contacts…</span>
            <kbd>⌘ K</kbd>
          </div>
          <div className="topbar-actions">
            <Link className="ai-action" href="/agents"><Sparkles size={15} /> Create with AI</Link>
            <button className="icon-button" aria-label="Notifications"><Bell size={18} /></button>
            <button className="profile-menu"><span>VV</span><div><strong>Vimal</strong><small>Owner</small></div><ChevronDown size={14} /></button>
          </div>
        </header>
        <main className="main-content">{children}</main>
      </div>
    </div>
  );
}
