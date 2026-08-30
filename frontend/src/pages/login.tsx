import { useEffect, useState } from 'react';
import Head from 'next/head';
import { useRouter } from 'next/router';
import { api, type RegistrationPolicy } from '@/lib/api';

function safeLocalPath(value: unknown): string {
  if (typeof value !== 'string' || !value.startsWith('/') || /[\\\u0000-\u001f\u007f]/.test(value)) {
    return '/';
  }

  try {
    const destination = new URL(value, window.location.origin);
    if (destination.origin !== window.location.origin || value.startsWith('//')) return '/';
    return `${destination.pathname}${destination.search}${destination.hash}`;
  } catch {
    return '/';
  }
}

export default function Login() {
  const router = useRouter();
  const [isRegister, setIsRegister] = useState(false);
  const [form, setForm] = useState({
    email: '', password: '', full_name: '', tenant_name: '', tenant_slug: '',
  });
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [registrationPolicy, setRegistrationPolicy] = useState<RegistrationPolicy | null>(null);
  const [registrationPolicyError, setRegistrationPolicyError] = useState('');

  useEffect(() => {
    let active = true;
    api.getRegistrationPolicy()
      .then((policy) => {
        if (!active) return;
        setRegistrationPolicy(policy);
        setRegistrationPolicyError('');
        if (!policy.registration_available) setIsRegister(false);
      })
      .catch(() => {
        if (!active) return;
        setRegistrationPolicy(null);
        setRegistrationPolicyError(
          'Account creation status is unavailable. Sign in or ask a workspace owner for an invitation.',
        );
        setIsRegister(false);
      });
    return () => { active = false; };
  }, []);

  const registrationAvailable = registrationPolicy?.registration_available === true;
  const registrationMessage = registrationPolicyError
    || registrationPolicy?.message
    || 'Checking account creation policy…';

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setSubmitting(true);
    try {
      if (isRegister) {
        await api.register(form);
      } else {
        await api.login(form.email, form.password);
      }
      await router.replace(safeLocalPath(router.query.next));
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : 'Authentication failed.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <Head><title>{isRegister ? 'Create account' : 'Sign in'} | VAV Voice AI</title></Head>
      <main className="auth-shell">
        <div className="auth-container">
          <h1>VAV Voice AI</h1>
          <p>Enterprise voice agent operations</p>

          <div className="card auth-card">
            <div className="auth-tabs" aria-label="Authentication mode">
              <button
                type="button"
                className={`btn ${!isRegister ? 'btn-primary' : 'btn-secondary'}`}
                aria-pressed={!isRegister}
                onClick={() => { setIsRegister(false); setError(''); }}
              >Sign in</button>
              <button
                type="button"
                className={`btn ${isRegister ? 'btn-primary' : 'btn-secondary'}`}
                aria-pressed={isRegister}
                aria-describedby="registration-policy-message"
                disabled={!registrationAvailable}
                onClick={() => { setIsRegister(true); setError(''); }}
              >Create account</button>
            </div>

            <p
              id="registration-policy-message"
              className="auth-policy-message"
              role="status"
            >{registrationMessage}</p>

            <form onSubmit={handleSubmit}>
              {isRegister ? (
                <>
                  <div className="form-group">
                    <label htmlFor="full-name">Full name</label>
                    <input
                      id="full-name"
                      name="name"
                      autoComplete="name"
                      value={form.full_name}
                      onChange={(event) => setForm({ ...form, full_name: event.target.value })}
                      required
                    />
                  </div>
                  <div className="form-group">
                    <label htmlFor="company-name">Company name</label>
                    <input
                      id="company-name"
                      name="organization"
                      autoComplete="organization"
                      value={form.tenant_name}
                      onChange={(event) => setForm({
                        ...form,
                        tenant_name: event.target.value,
                        tenant_slug: event.target.value
                          .toLowerCase()
                          .replace(/[^a-z0-9]+/g, '-')
                          .replace(/^-|-$/g, ''),
                      })}
                      required
                    />
                  </div>
                </>
              ) : null}
              <div className="form-group">
                <label htmlFor="email">Work email</label>
                <input
                  id="email"
                  name="email"
                  type="email"
                  autoComplete="email"
                  inputMode="email"
                  value={form.email}
                  onChange={(event) => setForm({ ...form, email: event.target.value })}
                  required
                />
              </div>
              <div className="form-group">
                <label htmlFor="password">Password</label>
                <input
                  id="password"
                  name="password"
                  type="password"
                  autoComplete={isRegister ? 'new-password' : 'current-password'}
                  value={form.password}
                  onChange={(event) => setForm({ ...form, password: event.target.value })}
                  required
                />
              </div>

              {error ? <div className="auth-error" role="alert">{error}</div> : null}

              <button type="submit" className="btn btn-primary auth-submit" disabled={submitting}>
                {submitting ? 'Please wait…' : isRegister ? 'Create account' : 'Sign in'}
              </button>
            </form>
          </div>
        </div>
      </main>
    </>
  );
}
