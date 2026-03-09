import Layout from '@/components/Layout';

export default function Settings() {
  return (
    <Layout>
      <div className="page-header">
        <h1>Settings</h1>
      </div>

      <div style={{ display: 'grid', gap: 24 }}>
        <div className="card">
          <h3 style={{ marginBottom: 16 }}>Tenant Configuration</h3>
          <div className="form-group">
            <label>Organization Name</label>
            <input placeholder="Your Company" />
          </div>
          <div className="form-group">
            <label>Default Caller ID</label>
            <input placeholder="+1234567890" />
          </div>
          <div className="form-group">
            <label>Default Timezone</label>
            <select>
              <option>UTC</option>
              <option>US/Eastern</option>
              <option>US/Central</option>
              <option>US/Pacific</option>
              <option>Europe/London</option>
            </select>
          </div>
          <button className="btn btn-primary">Save Settings</button>
        </div>

        <div className="card">
          <h3 style={{ marginBottom: 16 }}>API Keys</h3>
          <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 12 }}>
            Use API keys to integrate Voice AI Agent with your applications.
          </p>
          <button className="btn btn-primary">Generate API Key</button>
        </div>

        <div className="card">
          <h3 style={{ marginBottom: 16 }}>Telephony Provider</h3>
          <div className="form-group">
            <label>Provider</label>
            <select>
              <option>Twilio</option>
              <option disabled>Vonage (Coming Soon)</option>
              <option disabled>Plivo (Coming Soon)</option>
            </select>
          </div>
          <div className="form-group">
            <label>Account SID</label>
            <input type="password" placeholder="AC..." />
          </div>
          <div className="form-group">
            <label>Auth Token</label>
            <input type="password" placeholder="..." />
          </div>
          <button className="btn btn-primary">Save Provider Config</button>
        </div>
      </div>
    </Layout>
  );
}
