import { FormEvent, useEffect, useState } from 'react';
import { api, Integration } from '@/lib/api';
import styles from './McpConnections.module.css';

interface McpTool {
  name: string;
  description: string;
  read_only: boolean;
  input_schema: Record<string, unknown>;
}
interface McpStatus { status?: string; checked_at?: string; latency_ms?: number; error?: string }
const errorText = (error: unknown) => error instanceof Error ? error.message : 'MCP request failed';
const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];

export default function McpConnections({ canManage }: { canManage: boolean }) {
  const [connections, setConnections] = useState<Integration[]>([]);
  const [agents, setAgents] = useState<Array<{ id: string; name: string; eligible: boolean; reason: string }>>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState('');
  const [editing, setEditing] = useState<Integration | null | undefined>(undefined);

  const reload = async () => {
    const [items, options] = await Promise.all([api.listIntegrations(), api.listMcpAgents()]);
    setConnections(items.filter((item) => item.integration_type === 'mcp'));
    setAgents(options);
  };
  useEffect(() => {
    let active = true;
    Promise.all([api.listIntegrations(), api.listMcpAgents()]).then(([items, options]) => {
      if (!active) return;
      setConnections(items.filter((item) => item.integration_type === 'mcp'));
      setAgents(options);
    }).catch((e) => { if (active) setError(errorText(e)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  const run = async (key: string, work: () => Promise<void>) => {
    setBusy(key); setError(''); setNotice('');
    try { await work(); } catch (e) { setError(errorText(e)); } finally { setBusy(''); }
  };

  const save = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const config: Record<string, unknown> = {
      company_label: String(data.get('company_label') || '').trim(),
      auth_type: data.get('auth_type'),
      timeout_seconds: Number(data.get('timeout_seconds')),
    };
    const url = String(data.get('url') || '').trim();
    const credential = String(data.get('credential') || '').trim();
    if (url) config.url = url;
    if (credential) config.credential = credential;
    void run('save', async () => {
      const name = String(data.get('name') || '').trim();
      if (editing) await api.updateIntegration(editing.id, { name, config });
      else await api.createIntegration({ name, integration_type: 'mcp', config });
      setEditing(undefined);
      await reload();
      setNotice('Connection saved. Test and discover tools next. No business operation was executed. Changing the endpoint or authentication removes existing grants.');
    });
  };

  const permissions = (event: FormEvent<HTMLFormElement>, connection: Integration) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    void run(connection.id, async () => {
      await api.updateIntegration(connection.id, { config: {
        allowed_tools: data.getAll('allowed_tools'), agent_ids: data.getAll('agent_ids'),
        public_data_approved: data.get('public_data_approved') === 'on',
      } });
      await reload();
      setNotice('Permissions saved. Start a new call to load newly granted tools. Revocations are checked before each lookup.');
    });
  };

  return <section id="mcp-connections" className={`card integration-form-panel ${styles.panel}`} aria-labelledby="mcp-title">
    <div className="integration-form-heading"><div>
      <span className="page-kicker">Controlled live data</span>
      <h2 id="mcp-title">MCP connections</h2>
      <p>Connect Hermes or another MCP server using HTTPS Streamable HTTP. Credentials are encrypted and never returned to the browser.</p>
    </div>{canManage && <button className="btn btn-primary" disabled={!!busy} onClick={() => setEditing(null)}>Add MCP connection</button>}</div>
    <p className="integration-access-note">Read-only public business data only. Patient records, customer balances, bookings and ERP updates require separate identity and transaction controls and are not enabled here. Use a company-scoped, read-only server credential. Local stdio, private-network URLs, legacy SSE and interactive OAuth are not supported in this release.</p>
    {error && <div role="alert" className="auth-error">{error} <button className="btn btn-secondary" disabled={!!busy} onClick={() => void run('reload', async () => { await reload(); setLoading(false); })}>Reload connections</button></div>}
    {notice && <p role="status">{notice}</p>}
    {loading && <p role="status">Loading MCP connections…</p>}
    {editing !== undefined && canManage && <form key={editing?.id || 'new'} onSubmit={save} className="builder-form">
      <fieldset disabled={!!busy}>
        <legend>{editing ? 'Edit MCP connection' : 'New MCP connection'}</legend>
        <label>Name<input name="name" required maxLength={255} defaultValue={editing?.name || ''} /></label>
        <label>Company scope<input name="company_label" required maxLength={160} defaultValue={String(editing?.config.company_label || '')} placeholder="Company whose public data this connection exposes" /></label>
        <label>MCP server URL<input name="url" type="url" required={!editing} placeholder={editing ? 'Saved securely — leave blank to keep' : 'https://your-server.example/mcp'} /></label>
        <label>Authentication<select name="auth_type" defaultValue={String(editing?.config.auth_type || 'bearer')}><option value="bearer">Bearer token</option><option value="none">No authentication (public server)</option></select></label>
        <label>Bearer token<input name="credential" type="password" autoComplete="new-password" maxLength={4096} placeholder={editing ? 'Leave blank to keep existing token' : 'Stored encrypted'} /></label>
        <label>Operation timeout (seconds)<input name="timeout_seconds" type="number" min={2} max={30} required defaultValue={Number(editing?.config.timeout_seconds || 10)} /></label>
        <button className="btn btn-primary" type="submit">{busy === 'save' ? 'Saving…' : 'Save connection'}</button>
        <button className="btn btn-secondary" type="button" onClick={() => setEditing(undefined)}>Cancel</button>
      </fieldset>
    </form>}
    {!loading && !connections.length && <p>No MCP connections yet. Adding a connection does not grant access to any agent.</p>}
    {connections.map((connection) => {
      const tools = (connection.config.tools || []) as McpTool[];
      const status = (connection.config.last_test || {}) as McpStatus;
      const allowed = strings(connection.config.allowed_tools);
      const granted = strings(connection.config.agent_ids);
      return <article key={connection.id} className="card" style={{ marginTop: 16, padding: 16 }}>
        <h3>{connection.name}</h3>
        <p>{String(connection.config.company_label || '')} · {connection.is_active ? 'Enabled' : 'Disabled'} · {status.status || 'Untested'}</p>
        {status.checked_at && <p>Last connection test: {new Date(status.checked_at).toLocaleString()}{status.latency_ms !== undefined ? ` · ${status.latency_ms} ms for handshake and discovery (not call latency)` : ''}</p>}
        {status.error && <p role="alert">{status.error}</p>}
        {canManage && <div className="form-actions">
          <button className="btn btn-secondary" disabled={!!busy} onClick={() => void run(connection.id, async () => {
            const tested = await api.testMcpConnection(connection.id);
            await reload();
            const result = tested.config.last_test as McpStatus;
            setNotice(result.status === 'connected' ? 'Connected and discovered tools. No tool was executed. Review permissions below.' : 'Connection test failed. Access grants were cleared; check the endpoint and credential.');
          })}>{busy === connection.id ? 'Working…' : 'Test connection & discover tools'}</button>
          <button className="btn btn-secondary" disabled={!!busy} onClick={() => setEditing(connection)}>Edit</button>
          <button className="btn btn-secondary" disabled={!!busy} onClick={() => void run(connection.id, async () => { await api.updateIntegration(connection.id, { is_active: !connection.is_active }); await reload(); })}>{connection.is_active ? 'Disable' : 'Enable'}</button>
          <button className="btn btn-danger" disabled={!!busy} onClick={() => {
            if (window.confirm(`Delete ${connection.name}? This revokes future MCP lookups.`)) void run(connection.id, async () => { await api.deleteIntegration(connection.id); await reload(); });
          }}>Delete</button>
        </div>}
        {!!tools.length && <form key={JSON.stringify(connection.config)} onSubmit={(event) => permissions(event, connection)}>
          <fieldset disabled={!canManage || !!busy || status.status !== 'connected'}>
            <legend>Explicit tool and agent permissions</legend>
            <p>Server read-only labels are claims, not guarantees. Approve only tools you have reviewed. Credentials on the MCP server must enforce the company boundary. Each call loads up to 20 tools across its MCP connections.</p>
            {tools.map((tool) => <div key={tool.name}>
              <label><input type="checkbox" name="allowed_tools" value={tool.name} defaultChecked={allowed.includes(tool.name)} disabled={!tool.read_only} /> {tool.name} — {tool.read_only ? 'Read-only declared; requires your approval' : 'Blocked: write or unknown safety'}</label>
              <p>{tool.description}</p><details><summary>Input schema</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(tool.input_schema, null, 2)}</pre></details>
            </div>)}
            <h4>Allowed voice agents</h4>
            <p>The accepted single-pass receptionist remains unchanged. Only enabled LiveKit/Inworld realtime tool-loop agents can use these tools. Start a new call after granting access.</p>
            {agents.map((agent) => <label key={agent.id} style={{ display: 'block' }}><input type="checkbox" name="agent_ids" value={agent.id} defaultChecked={granted.includes(agent.id) && agent.eligible} disabled={!agent.eligible} /> {agent.name}{!agent.eligible && ` — ${agent.reason}`}</label>)}
            <label style={{ display: 'block', marginTop: 12 }}><input type="checkbox" name="public_data_approved" defaultChecked={connection.config.public_data_approved === true} /> I have reviewed the selected tools and confirm they expose only public-safe company information, not private customer/patient/account data.</label>
            {canManage && <button className="btn btn-primary" type="submit">Save permissions</button>}
          </fieldset>
        </form>}
      </article>;
    })}
  </section>;
}
