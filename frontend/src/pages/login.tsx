import { useState } from 'react';
import { useRouter } from 'next/router';
import { api } from '@/lib/api';

export default function Login() {
  const router = useRouter();
  const [isRegister, setIsRegister] = useState(false);
  const [form, setForm] = useState({
    email: '', password: '', full_name: '', tenant_name: '', tenant_slug: '',
  });
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    try {
      if (isRegister) {
        await api.register(form);
      } else {
        await api.login(form.email, form.password);
      }
      router.push('/');
    } catch (err: any) {
      setError(err.message);
    }
  };

  return (
    <div style={{
      minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'var(--bg-primary)',
    }}>
      <div style={{ width: 400 }}>
        <h1 style={{ textAlign: 'center', marginBottom: 8, color: 'var(--accent)' }}>Voice AI Agent</h1>
        <p style={{ textAlign: 'center', color: 'var(--text-secondary)', marginBottom: 32 }}>
          Enterprise Voice AI Platform
        </p>

        <div className="card">
          <div style={{ display: 'flex', gap: 8, marginBottom: 24 }}>
            <button
              className={`btn ${!isRegister ? 'btn-primary' : 'btn-secondary'}`}
              style={{ flex: 1, justifyContent: 'center' }}
              onClick={() => setIsRegister(false)}
            >Sign In</button>
            <button
              className={`btn ${isRegister ? 'btn-primary' : 'btn-secondary'}`}
              style={{ flex: 1, justifyContent: 'center' }}
              onClick={() => setIsRegister(true)}
            >Register</button>
          </div>

          <form onSubmit={handleSubmit}>
            {isRegister && (
              <>
                <div className="form-group">
                  <label>Full Name</label>
                  <input value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} required />
                </div>
                <div className="form-group">
                  <label>Company Name</label>
                  <input value={form.tenant_name} onChange={(e) => setForm({
                    ...form, tenant_name: e.target.value,
                    tenant_slug: e.target.value.toLowerCase().replace(/[^a-z0-9]+/g, '-'),
                  })} required />
                </div>
              </>
            )}
            <div className="form-group">
              <label>Email</label>
              <input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required />
            </div>
            <div className="form-group">
              <label>Password</label>
              <input type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required />
            </div>

            {error && (
              <div style={{ color: 'var(--danger)', fontSize: 13, marginBottom: 12 }}>{error}</div>
            )}

            <button type="submit" className="btn btn-primary" style={{ width: '100%', justifyContent: 'center', padding: '10px 16px' }}>
              {isRegister ? 'Create Account' : 'Sign In'}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
