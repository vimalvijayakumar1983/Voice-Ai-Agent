import Head from 'next/head';
import Link from 'next/link';
import { useRouter } from 'next/router';
import type { FormEvent } from 'react';
import { useEffect, useState } from 'react';
import { AudioWaveform, ShieldCheck } from 'lucide-react';
import { api } from '@/lib/api';

export default function AcceptInvite() {
  const router = useRouter();
  const [token] = useState(() => {
    if (typeof window === 'undefined') return '';
    const fragmentToken = new URLSearchParams(window.location.hash.slice(1)).get('token');
    const legacyQueryToken = new URLSearchParams(window.location.search).get('token');
    return fragmentToken || legacyQueryToken || '';
  });
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!router.isReady) return;
    // Remove the secret from the address bar and browser history immediately.
    if (token) {
      window.history.replaceState(null, '', '/accept-invite');
    }
  }, [router.isReady, token]);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError('');
    if (!token) {
      setError('This invitation link is incomplete. Ask your workspace administrator for a new link.');
      return;
    }
    if (password.length < 8) {
      setError('Use at least 8 characters for your password.');
      return;
    }
    if (password !== confirmation) {
      setError('The passwords do not match.');
      return;
    }

    setSubmitting(true);
    try {
      await api.acceptInvitation(token, password);
      await router.replace('/');
    } catch (caught: unknown) {
      setError(caught instanceof Error ? caught.message : 'This invitation could not be accepted.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <Head><title>Join workspace | VAV Voice AI</title></Head>
      <main className="auth-shell">
        <div className="auth-container invite-auth-container">
          <div className="auth-brand-mark" aria-hidden="true">
            <AudioWaveform size={22} strokeWidth={2.4} />
          </div>
          <h1>Join your voice workspace</h1>
          <p>Set a password to activate your secure team account.</p>

          <section className="card auth-card" aria-labelledby="accept-invite-title">
            <div className="invite-assurance">
              <ShieldCheck size={18} aria-hidden="true" />
              <div>
                <h2 id="accept-invite-title">Accept invitation</h2>
                <p>This link can be used once and expires after seven days.</p>
              </div>
            </div>

            {!router.isReady ? (
              <div className="page-loading" role="status">Checking invitation…</div>
            ) : token ? (
              <form onSubmit={handleSubmit}>
                <div className="form-group">
                  <label htmlFor="invite-password">Create password</label>
                  <input
                    id="invite-password"
                    type="password"
                    autoComplete="new-password"
                    minLength={8}
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    aria-describedby="invite-password-hint"
                    required
                  />
                  <p id="invite-password-hint" className="form-hint">At least 8 characters.</p>
                </div>
                <div className="form-group">
                  <label htmlFor="invite-password-confirmation">Confirm password</label>
                  <input
                    id="invite-password-confirmation"
                    type="password"
                    autoComplete="new-password"
                    minLength={8}
                    value={confirmation}
                    onChange={(event) => setConfirmation(event.target.value)}
                    required
                  />
                </div>

                {error ? <div className="auth-error" role="alert">{error}</div> : null}
                <button type="submit" className="btn btn-primary auth-submit" disabled={submitting}>
                  {submitting ? 'Activating account…' : 'Join workspace'}
                </button>
              </form>
            ) : (
              <div className="invite-invalid" role="alert">
                <strong>Invitation token missing</strong>
                <p>Open the complete invitation link, or ask an administrator to create a new one.</p>
                <Link href="/login" className="btn btn-secondary">Go to sign in</Link>
              </div>
            )}
          </section>
        </div>
      </main>
    </>
  );
}
