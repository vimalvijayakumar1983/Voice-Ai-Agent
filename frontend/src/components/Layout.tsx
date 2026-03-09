import Link from 'next/link';
import { useRouter } from 'next/router';
import { ReactNode } from 'react';

const navItems = [
  { href: '/', label: 'Dashboard', icon: '/' },
  { href: '/agents', label: 'Agents', icon: 'A' },
  { href: '/calls', label: 'Call Logs', icon: 'C' },
  { href: '/workflows', label: 'Workflows', icon: 'W' },
  { href: '/campaigns', label: 'Campaigns', icon: 'P' },
  { href: '/compliance', label: 'Compliance', icon: 'S' },
  { href: '/integrations', label: 'Integrations', icon: 'I' },
  { href: '/billing', label: 'Billing', icon: 'B' },
  { href: '/settings', label: 'Settings', icon: 'G' },
];

export default function Layout({ children }: { children: ReactNode }) {
  const router = useRouter();

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="sidebar-logo">Voice AI Agent</div>
        <nav className="sidebar-nav">
          {navItems.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={router.pathname === item.href ? 'active' : ''}
            >
              <span style={{ width: 20, textAlign: 'center', opacity: 0.6 }}>{item.icon}</span>
              {item.label}
            </Link>
          ))}
        </nav>
      </aside>
      <main className="main-content">{children}</main>
    </div>
  );
}
