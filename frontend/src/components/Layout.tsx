import Link from 'next/link';
import Head from 'next/head';
import { useRouter } from 'next/router';
import { ReactNode, useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity,
  AudioWaveform,
  Bot,
  BookOpenCheck,
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
import shellAccessibility from '@/lib/shell-accessibility.cjs';

const { focusTrapTarget, pageTitleForPath } = shellAccessibility;

const navigation = [
  {
    label: 'Workspace',
    items: [
      { href: '/', label: 'Overview', icon: Activity },
      { href: '/agents', label: 'Agents', icon: Bot },
      { href: '/knowledge', label: 'Knowledge', icon: BookOpenCheck },
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

const focusableSelector = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
  '[contenteditable="true"]',
].join(',');

function getFocusableElements(container: HTMLElement | null) {
  if (!container) return [];
  return Array.from(container.querySelectorAll<HTMLElement>(focusableSelector)).filter(
    (element) => !element.hidden && element.getAttribute('aria-hidden') !== 'true',
  );
}

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
  const [isMobileViewport, setIsMobileViewport] = useState(false);
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [profileOpen, setProfileOpen] = useState(false);
  const profileRef = useRef<HTMLDivElement>(null);
  const profileButtonRef = useRef<HTMLButtonElement>(null);
  const profileMenuItemRef = useRef<HTMLButtonElement>(null);
  const sidebarRef = useRef<HTMLElement>(null);
  const mobileMenuButtonRef = useRef<HTMLButtonElement>(null);
  const mobileCloseButtonRef = useRef<HTMLButtonElement>(null);
  const mainRef = useRef<HTMLElement>(null);
  const restoreMobileFocusRef = useRef(true);
  const focusMainAfterRouteRef = useRef(false);

  const closeMobileNavigation = useCallback((restoreFocus = true) => {
    restoreMobileFocusRef.current = restoreFocus;
    setMobileOpen(false);
  }, []);

  const openMobileNavigation = () => {
    restoreMobileFocusRef.current = true;
    setProfileOpen(false);
    setMobileOpen(true);
  };

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
    const mediaQuery = window.matchMedia('(max-width: 900px)');
    const updateViewport = () => {
      setIsMobileViewport(mediaQuery.matches);
      if (!mediaQuery.matches) setMobileOpen(false);
    };

    updateViewport();
    mediaQuery.addEventListener('change', updateViewport);
    return () => mediaQuery.removeEventListener('change', updateViewport);
  }, []);

  useEffect(() => {
    if (!mobileOpen) return;

    const previousOverflow = document.body.style.overflow;
    const navigationTrigger = mobileMenuButtonRef.current;
    document.body.style.overflow = 'hidden';
    const focusFrame = window.requestAnimationFrame(() => mobileCloseButtonRef.current?.focus());

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeMobileNavigation();
        return;
      }
      if (event.key !== 'Tab') return;

      const focusable = getFocusableElements(sidebarRef.current);
      if (!focusable.length) {
        event.preventDefault();
        return;
      }

      const activeIndex = focusable.indexOf(document.activeElement as HTMLElement);
      const targetIndex = focusTrapTarget(activeIndex, focusable.length, event.shiftKey);
      if (targetIndex !== null) {
        event.preventDefault();
        focusable[targetIndex]?.focus();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener('keydown', handleKeyDown);
      document.body.style.overflow = previousOverflow;
      if (restoreMobileFocusRef.current) {
        window.requestAnimationFrame(() => navigationTrigger?.focus());
      }
    };
  }, [closeMobileNavigation, mobileOpen]);

  useEffect(() => {
    if (!profileOpen) return;

    const handlePointerDown = (event: PointerEvent) => {
      if (!profileRef.current?.contains(event.target as Node)) setProfileOpen(false);
    };
    document.addEventListener('pointerdown', handlePointerDown);
    const focusFrame = window.requestAnimationFrame(() => profileMenuItemRef.current?.focus());
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener('pointerdown', handlePointerDown);
    };
  }, [profileOpen]);

  useEffect(() => {
    const handleRouteComplete = () => {
      setProfileOpen(false);
      setMobileOpen(false);
      if (focusMainAfterRouteRef.current) {
        focusMainAfterRouteRef.current = false;
        window.requestAnimationFrame(() => mainRef.current?.focus());
      }
    };

    router.events.on('routeChangeComplete', handleRouteComplete);
    return () => router.events.off('routeChangeComplete', handleRouteComplete);
  }, [router.events]);

  const logout = () => {
    setProfileOpen(false);
    api.logout();
  };

  const isActive = (href: string) =>
    href === '/' ? router.pathname === '/' : router.pathname.startsWith(href);

  const handleNavigation = (href: string) => {
    const currentPath = router.asPath.split(/[?#]/, 1)[0];
    focusMainAfterRouteRef.current = currentPath !== href;
    closeMobileNavigation(false);
    setProfileOpen(false);
    if (currentPath === href) {
      window.requestAnimationFrame(() => mainRef.current?.focus());
    }
  };

  const handleProfileTriggerKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      setProfileOpen(true);
    }
  };

  const handleProfileMenuKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      setProfileOpen(false);
      window.requestAnimationFrame(() => profileButtonRef.current?.focus());
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      profileMenuItemRef.current?.focus();
    } else if (event.key === 'Tab') {
      setProfileOpen(false);
    }
  };

  const pageTitle = pageTitleForPath(router.pathname);

  return (
    <>
      <Head><title>{pageTitle} | VAV Voice AI</title></Head>
      <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <aside
        ref={sidebarRef}
        id="primary-navigation-drawer"
        className={`sidebar ${mobileOpen ? 'sidebar-open' : ''}`}
        aria-label={isMobileViewport ? 'Navigation menu' : 'Workspace navigation'}
        aria-hidden={isMobileViewport && !mobileOpen ? true : undefined}
        aria-modal={isMobileViewport && mobileOpen ? true : undefined}
        role={isMobileViewport && mobileOpen ? 'dialog' : undefined}
        inert={isMobileViewport && !mobileOpen}
      >
        <div className="brand-lockup">
          <div className="brand-mark"><AudioWaveform size={20} strokeWidth={2.4} aria-hidden="true" /></div>
          <div>
            <strong>VAV Voice AI</strong>
            <span>Enterprise workspace</span>
          </div>
          <button ref={mobileCloseButtonRef} className="icon-button mobile-close" onClick={() => closeMobileNavigation()} aria-label="Close navigation menu">
            <X size={18} aria-hidden="true" />
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
                    onClick={() => handleNavigation(item.href)}
                  >
                    <Icon size={17} strokeWidth={1.9} aria-hidden="true" />
                    <span>{item.label}</span>
                    {item.href === '/playground' && <span className="nav-new">Live</span>}
                  </Link>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className="provider-pill"><span className="status-dot" aria-hidden="true" /> Smallest.ai native</div>
          <p>Powered by Atoms, Pulse & Lightning</p>
        </div>
      </aside>

      {mobileOpen ? (
        <button
          type="button"
          className="sidebar-backdrop"
          onClick={() => closeMobileNavigation()}
          aria-hidden="true"
          tabIndex={-1}
        />
      ) : null}

      <div className="app-main" inert={isMobileViewport && mobileOpen}>
        <header className="topbar">
          <button
            ref={mobileMenuButtonRef}
            className="icon-button mobile-menu"
            onClick={openMobileNavigation}
            aria-label="Open navigation menu"
            aria-controls="primary-navigation-drawer"
            aria-expanded={mobileOpen}
          >
            <Menu size={19} aria-hidden="true" />
          </button>
          <div className="topbar-context" role="status" aria-live="polite">
            <span className="status-dot" aria-hidden="true" />
            <span>Secure production workspace</span>
          </div>
          <div className="topbar-actions">
            <Link className="ai-action" href="/agents" onClick={() => handleNavigation('/agents')}><Sparkles size={15} aria-hidden="true" /> Create with AI</Link>
            <div className="profile-control" ref={profileRef}>
              <button
                ref={profileButtonRef}
                type="button"
                className="profile-menu"
                aria-expanded={profileOpen}
                aria-haspopup="menu"
                aria-controls="profile-account-menu"
                onClick={() => setProfileOpen((open) => !open)}
                onKeyDown={handleProfileTriggerKeyDown}
              >
                <span>{getInitials(user)}</span>
                <div><strong>{user?.full_name || 'Your account'}</strong><small>{formatRole(user?.role)}</small></div>
                <ChevronDown size={14} aria-hidden="true" />
              </button>
              {profileOpen ? (
                <div id="profile-account-menu" className="profile-popover" role="menu" aria-label="Account" onKeyDown={handleProfileMenuKeyDown}>
                  <div className="profile-popover-identity" role="presentation" aria-hidden="true">
                    <strong>{user?.full_name || 'Signed-in user'}</strong>
                    <span>{user?.email || 'Loading account…'}</span>
                  </div>
                  <button ref={profileMenuItemRef} type="button" role="menuitem" onClick={logout}>
                    <LogOut size={15} aria-hidden="true" /> Sign out
                  </button>
                </div>
              ) : null}
            </div>
          </div>
        </header>
        <main ref={mainRef} id="main-content" className="main-content" tabIndex={-1}>{children}</main>
      </div>
      </div>
    </>
  );
}
