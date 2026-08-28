import NextApp, { type AppContext, type AppProps } from 'next/app';
import Head from 'next/head';
import { useRouter } from 'next/router';
import { useEffect, useState } from 'react';
import { AudioWaveform } from 'lucide-react';
import { api } from '@/lib/api';
import '@/styles/globals.css';

export default function App({ Component, pageProps }: AppProps) {
  const router = useRouter();
  const isPublicRoute = router.pathname === '/login' || router.pathname === '/accept-invite';
  const [sessionVerified, setSessionVerified] = useState(false);

  useEffect(() => {
    const handleExpiredSession = () => {
      setSessionVerified(false);
      if (router.pathname !== '/login' && router.pathname !== '/accept-invite') {
        void router.replace({ pathname: '/login', query: { next: router.asPath } });
      }
    };

    window.addEventListener('vav:auth-expired', handleExpiredSession);
    const handleCommittedSession = () => setSessionVerified(false);
    window.addEventListener('vav:session-committed', handleCommittedSession);
    return () => {
      window.removeEventListener('vav:auth-expired', handleExpiredSession);
      window.removeEventListener('vav:session-committed', handleCommittedSession);
    };
  }, [router]);

  useEffect(() => {
    if (!router.isReady || isPublicRoute) return;

    if (sessionVerified) return;

    let active = true;
    api.getMe()
      .then(() => {
        if (active) setSessionVerified(true);
      })
      .catch(() => {
        if (active && !isPublicRoute) {
          void router.replace({ pathname: '/login', query: { next: router.asPath } });
        }
      });

    return () => {
      active = false;
    };
  }, [isPublicRoute, router, sessionVerified]);

  return (
    <>
      <Head>
        <title>VAV Voice AI</title>
        <meta name="description" content="Enterprise voice agent operations, analytics, and automation." />
      </Head>
      {isPublicRoute || sessionVerified ? (
        <Component {...pageProps} />
      ) : (
        <div className="session-gate" role="status" aria-live="polite">
          <div className="session-gate-mark"><AudioWaveform size={22} /></div>
          <strong>Securing your workspace</strong>
          <span>Checking your session…</span>
        </div>
      )}
    </>
  );
}

// A per-request CSP nonce cannot be attached to build-time static HTML. Opting
// Pages Router rendering into the server path lets proxy.ts pass a fresh nonce
// to _document for every HTML response.
App.getInitialProps = async (context: AppContext) => NextApp.getInitialProps(context);
