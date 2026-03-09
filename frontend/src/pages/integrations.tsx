import Layout from '@/components/Layout';

export default function Integrations() {
  const integrationTypes = [
    { name: 'Webhooks', description: 'Receive real-time events via HTTP webhooks', status: 'Available' },
    { name: 'HubSpot CRM', description: 'Sync contacts and call logs with HubSpot', status: 'Coming Soon' },
    { name: 'Salesforce', description: 'Bi-directional sync with Salesforce CRM', status: 'Coming Soon' },
    { name: 'Zapier', description: 'Connect to 5000+ apps via Zapier', status: 'Coming Soon' },
    { name: 'Slack', description: 'Get call notifications in Slack channels', status: 'Coming Soon' },
    { name: 'Google Sheets', description: 'Export call data to Google Sheets', status: 'Coming Soon' },
  ];

  return (
    <Layout>
      <div className="page-header">
        <h1>Integrations</h1>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 16 }}>
        {integrationTypes.map((integration) => (
          <div key={integration.name} className="card" style={{ display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                <h3 style={{ fontSize: 16 }}>{integration.name}</h3>
                <span className={`badge ${integration.status === 'Available' ? 'badge-success' : 'badge-info'}`}>
                  {integration.status}
                </span>
              </div>
              <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>{integration.description}</p>
            </div>
            <button
              className={`btn ${integration.status === 'Available' ? 'btn-primary' : 'btn-secondary'}`}
              style={{ marginTop: 16, width: '100%', justifyContent: 'center' }}
              disabled={integration.status !== 'Available'}
            >
              {integration.status === 'Available' ? 'Configure' : 'Coming Soon'}
            </button>
          </div>
        ))}
      </div>
    </Layout>
  );
}
