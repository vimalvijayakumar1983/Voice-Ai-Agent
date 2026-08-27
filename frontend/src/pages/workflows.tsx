import { useEffect, useState } from 'react';
import Layout from '@/components/Layout';
import { api, CallWorkflow } from '@/lib/api';

export default function Workflows() {
  const [workflows, setWorkflows] = useState<CallWorkflow[]>([]);

  useEffect(() => { api.listWorkflows().then(setWorkflows).catch(() => {}); }, []);

  return (
    <Layout>
      <div className="page-header">
        <h1>Call Workflows</h1>
        <button className="btn btn-primary">+ Create Workflow</button>
      </div>

      {workflows.length === 0 ? (
        <div className="empty-state">
          <h3>No workflows yet</h3>
          <p>Build call workflows to define how your agents handle conversations.</p>
          <p style={{ marginTop: 12, fontSize: 13 }}>
            Workflows let you define: greeting → AI conversation → transfer → hangup sequences,
            with branching logic and webhook triggers.
          </p>
        </div>
      ) : (
        <div className="table-container">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Trigger</th>
                <th>Nodes</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {workflows.map((w) => (
                <tr key={w.id}>
                  <td style={{ fontWeight: 500 }}>{w.name}</td>
                  <td>{w.trigger_type}</td>
                  <td>{w.nodes?.length || 0}</td>
                  <td>
                    <span className={`badge ${w.is_active ? 'badge-success' : 'badge-danger'}`}>
                      {w.is_active ? 'Active' : 'Inactive'}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Layout>
  );
}
