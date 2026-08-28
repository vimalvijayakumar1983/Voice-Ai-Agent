import { FormEvent, useEffect, useState } from 'react';
import {
  CheckCircle2,
  CircleAlert,
  KeyRound,
  Loader2,
  Pencil,
  Plus,
  RotateCw,
  ShieldCheck,
  Trash2,
  Webhook,
  X,
} from 'lucide-react';
import Layout from '@/components/Layout';
import { api, Integration } from '@/lib/api';

interface IntegrationForm {
  name: string;
  url: string;
  events: string[];
  signingSecret: string;
  replaceSecret: boolean;
  isActive: boolean;
}

interface PageNotice {
  type: 'success' | 'error';
  text: string;
}

const EMPTY_FORM: IntegrationForm = {
  name: '',
  url: '',
  events: ['call.completed'],
  signingSecret: '',
  replaceSecret: false,
  isActive: true,
};

const EVENT_OPTIONS = [
  {
    value: 'call.completed',
    label: 'Call completed',
    description: 'Status, duration, disposition, and direction after post-call processing.',
  },
  {
    value: 'call.analytics_updated',
    label: 'Call analytics updated',
    description: 'Authoritative duration or disposition corrections after provider analytics.',
  },
];

const PLANNED_INTEGRATIONS = [
  { name: 'HubSpot CRM', description: 'Contact and conversation synchronization.' },
  { name: 'Salesforce', description: 'Account, lead, and activity synchronization.' },
  { name: 'Zapier', description: 'No-code automation across connected business apps.' },
  { name: 'Slack', description: 'Operational alerts and review notifications.' },
  { name: 'Google Sheets', description: 'Governed call-data exports for operations teams.' },
];

function messageFrom(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function configString(integration: Integration, key: string) {
  const value = integration.config[key];
  return typeof value === 'string' ? value : '';
}

function configEvents(integration: Integration) {
  const value = integration.config.events;
  if (!Array.isArray(value)) return [];
  return value.filter((event): event is string => typeof event === 'string');
}

function validatePublicHttpsUrl(value: string) {
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== 'https:' || !parsed.hostname || parsed.username || parsed.password || parsed.hash) {
      return null;
    }
    return parsed.toString();
  } catch {
    return null;
  }
}

export default function Integrations() {
  const [integrations, setIntegrations] = useState<Integration[]>([]);
  const [canManage, setCanManage] = useState(false);
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);
  const [loadError, setLoadError] = useState('');
  const [notice, setNotice] = useState<PageNotice | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<IntegrationForm>(EMPTY_FORM);
  const [formError, setFormError] = useState('');
  const [working, setWorking] = useState('');

  useEffect(() => {
    let active = true;
    Promise.all([api.listIntegrations(), api.getMe()])
      .then(([items, user]) => {
        if (!active) return;
        setIntegrations(items.filter((item) => item.integration_type === 'webhook'));
        setCanManage(user.role === 'owner' || user.role === 'admin');
        setLoadError('');
      })
      .catch((error) => {
        if (active) setLoadError(messageFrom(error, 'Could not load webhook integrations.'));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [reloadKey]);

  const openCreate = () => {
    setEditingId(null);
    setForm(EMPTY_FORM);
    setFormError('');
    setNotice(null);
    setShowForm(true);
  };

  const openEdit = (integration: Integration) => {
    setEditingId(integration.id);
    setForm({
      name: integration.name,
      url: configString(integration, 'url'),
      events: configEvents(integration),
      signingSecret: '',
      replaceSecret: !integration.secret_fields.includes('signing_secret'),
      isActive: integration.is_active,
    });
    setFormError('');
    setNotice(null);
    setShowForm(true);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  const closeForm = () => {
    setShowForm(false);
    setEditingId(null);
    setForm(EMPTY_FORM);
    setFormError('');
  };

  const retryLoad = () => {
    setLoading(true);
    setLoadError('');
    setReloadKey((current) => current + 1);
  };

  const toggleEvent = (eventName: string) => {
    setForm((current) => ({
      ...current,
      events: current.events.includes(eventName)
        ? current.events.filter((item) => item !== eventName)
        : [...current.events, eventName],
    }));
  };

  const saveIntegration = async (event: FormEvent) => {
    event.preventDefault();
    setFormError('');
    setNotice(null);

    const name = form.name.trim();
    if (!name) {
      setFormError('Give this destination a recognizable name.');
      return;
    }
    const url = validatePublicHttpsUrl(form.url.trim());
    if (!url) {
      setFormError('Enter a public HTTPS URL without embedded credentials.');
      return;
    }
    if (form.events.length === 0) {
      setFormError('Select at least one event to deliver.');
      return;
    }
    const secretRequired = !editingId || form.replaceSecret;
    if (secretRequired && form.signingSecret.trim().length < 16) {
      setFormError('The signing secret must contain at least 16 non-whitespace characters.');
      return;
    }

    setWorking(editingId ? `save-${editingId}` : 'create');
    try {
      if (editingId) {
        const config: Record<string, unknown> = { url, events: form.events };
        if (form.replaceSecret) config.signing_secret = form.signingSecret;
        const updated = await api.updateIntegration(editingId, {
          name,
          config,
          is_active: form.isActive,
        });
        setIntegrations((current) => current.map((item) => (
          item.id === updated.id ? updated : item
        )));
        setNotice({ type: 'success', text: `${updated.name} was updated securely.` });
      } else {
        const staged = await api.createIntegration({
          name,
          integration_type: 'webhook',
          config: {
            url,
            // Creation currently defaults active server-side. Stage with no
            // subscriptions so no event can be delivered before status is set.
            events: [],
            signing_secret: form.signingSecret,
          },
        });
        let created: Integration;
        try {
          created = await api.updateIntegration(staged.id, {
            config: { events: form.events },
            is_active: form.isActive,
          });
        } catch (statusError) {
          setIntegrations((current) => [staged, ...current]);
          setNotice({
            type: 'error',
            text: `${staged.name} was staged with no event subscriptions, but final setup failed: ${messageFrom(statusError, 'status update failed')}`,
          });
          closeForm();
          return;
        }
        setIntegrations((current) => [created, ...current]);
        setNotice({
          type: 'success',
          text: `${created.name} was created${created.is_active ? ' and is ready for signed delivery' : ' as inactive'}.`,
        });
      }
      closeForm();
    } catch (error) {
      setFormError(messageFrom(error, 'Could not save the webhook destination.'));
      setReloadKey((current) => current + 1);
    } finally {
      setWorking('');
    }
  };

  const deleteIntegration = async (integration: Integration) => {
    if (!window.confirm(`Delete ${integration.name}? Future events will no longer be sent to this destination.`)) {
      return;
    }
    setWorking(`delete-${integration.id}`);
    setNotice(null);
    try {
      await api.deleteIntegration(integration.id);
      setIntegrations((current) => current.filter((item) => item.id !== integration.id));
      if (editingId === integration.id) closeForm();
      setNotice({ type: 'success', text: `${integration.name} was deleted.` });
    } catch (error) {
      setNotice({ type: 'error', text: messageFrom(error, 'Could not delete the destination.') });
    } finally {
      setWorking('');
    }
  };

  const editingIntegration = integrations.find((item) => item.id === editingId);
  const secretConfigured = editingIntegration?.secret_fields.includes('signing_secret') ?? false;

  return (
    <Layout>
      <div className="page-header">
        <div>
          <span className="page-kicker">Connect & automate</span>
          <h1>Integrations</h1>
          <p className="page-subtitle">
            Deliver signed post-call events to your systems without exposing stored credentials.
          </p>
        </div>
        {canManage && (
          <button className="btn btn-primary" onClick={showForm ? closeForm : openCreate}>
            {showForm ? <X size={14} /> : <Plus size={14} />}
            {showForm ? 'Close' : 'Add webhook'}
          </button>
        )}
      </div>

      {notice && (
        <div
          className={`provider-alert integration-notice ${notice.type === 'error' ? 'provider-alert-error' : ''}`}
          role={notice.type === 'error' ? 'alert' : 'status'}
        >
          {notice.type === 'success' ? <CheckCircle2 size={15} /> : <CircleAlert size={15} />}
          <span>{notice.text}</span>
          <button className="btn btn-ghost btn-sm" onClick={() => setNotice(null)} aria-label="Dismiss message">
            <X size={13} />
          </button>
        </div>
      )}

      {!loading && !canManage && !loadError && (
        <div className="integration-access-note" role="note">
          <ShieldCheck size={15} />
          <span>You can review destinations. An owner or admin can create, edit, or delete them.</span>
        </div>
      )}

      {showForm && canManage && (
        <section className="card integration-form-panel" aria-labelledby="integration-form-title">
          <div className="integration-form-heading">
            <span className="integration-heading-icon"><Webhook size={18} /></span>
            <div>
              <span className="page-kicker">Signed HTTPS delivery</span>
              <h2 id="integration-form-title">{editingId ? 'Edit webhook destination' : 'Add webhook destination'}</h2>
              <p>Secrets are write-only. Stored values are never returned to this browser.</p>
            </div>
          </div>

          {formError && <div className="auth-error integration-form-error" role="alert">{formError}</div>}

          <form onSubmit={saveIntegration}>
            <fieldset className="integration-fieldset" disabled={Boolean(working)}>
              <div className="form-grid">
                <div className="form-group">
                  <label htmlFor="integration-name">Destination name</label>
                  <input
                    id="integration-name"
                    required
                    maxLength={255}
                    autoComplete="off"
                    value={form.name}
                    placeholder="Production data warehouse"
                    onChange={(event) => setForm({ ...form, name: event.target.value })}
                  />
                </div>
                <div className="form-group">
                  <label htmlFor="integration-url">Public HTTPS URL</label>
                  <input
                    id="integration-url"
                    required
                    type="url"
                    inputMode="url"
                    autoComplete="url"
                    value={form.url}
                    placeholder="https://example.com/hooks/voice"
                    onChange={(event) => setForm({ ...form, url: event.target.value })}
                  />
                  <p className="form-hint">Private hosts, redirects, and embedded credentials are rejected by the server.</p>
                </div>
              </div>

              <fieldset className="event-selector">
                <legend>Events</legend>
                {EVENT_OPTIONS.map((option) => (
                  <label className="event-option" key={option.value}>
                    <input
                      type="checkbox"
                      checked={form.events.includes(option.value)}
                      onChange={() => toggleEvent(option.value)}
                    />
                    <span>
                      <strong>{option.label}</strong>
                      <small>{option.description}</small>
                    </span>
                  </label>
                ))}
              </fieldset>

              {editingId && (
                <label className="secret-replace-control">
                  <input
                    type="checkbox"
                    checked={form.replaceSecret}
                    onChange={(event) => setForm({
                      ...form,
                      replaceSecret: event.target.checked,
                      signingSecret: event.target.checked ? form.signingSecret : '',
                    })}
                  />
                  <KeyRound size={15} />
                  <span>
                    <strong>Replace signing secret</strong>
                    <small>{secretConfigured ? 'A secret is configured and remains unchanged unless you replace it.' : 'No stored secret was reported; configure one before saving.'}</small>
                  </span>
                </label>
              )}

              {(!editingId || form.replaceSecret) && (
                <div className="form-group integration-secret-field">
                  <label htmlFor="integration-secret">
                    Signing secret <span>{editingId ? 'replacement' : 'write-only'}</span>
                  </label>
                  <input
                    id="integration-secret"
                    required
                    type="password"
                    minLength={16}
                    autoComplete="new-password"
                    value={form.signingSecret}
                    placeholder="At least 16 characters"
                    onChange={(event) => setForm({ ...form, signingSecret: event.target.value })}
                  />
                  <p className="form-hint">Use this value to verify the HMAC signature on each exact request body.</p>
                </div>
              )}

              <label className="toggle-control">
                <input
                  type="checkbox"
                  checked={form.isActive}
                  onChange={(event) => setForm({ ...form, isActive: event.target.checked })}
                />
                <span aria-hidden="true" />
                <div>
                  <strong>Active destination</strong>
                  <small>{form.isActive ? 'Subscribed events are eligible for delivery.' : 'Keep configuration saved without sending events.'}</small>
                </div>
              </label>
            </fieldset>

            <div className="integration-form-actions">
              <button type="button" className="btn btn-secondary" onClick={closeForm} disabled={Boolean(working)}>Cancel</button>
              <button type="submit" className="btn btn-primary" disabled={Boolean(working)}>
                {working ? <Loader2 className="spin" size={14} /> : <ShieldCheck size={14} />}
                {working ? 'Saving…' : editingId ? 'Save securely' : 'Create destination'}
              </button>
            </div>
          </form>
        </section>
      )}

      {loading ? (
        <div className="page-loading" role="status"><Loader2 className="spin" size={16} /> Loading integrations…</div>
      ) : loadError ? (
        <div className="card settings-error" role="alert">
          <div>
            <strong>Integrations are unavailable</strong>
            <p>{loadError}</p>
          </div>
          <button className="btn btn-secondary" onClick={retryLoad}><RotateCw size={13} /> Retry</button>
        </div>
      ) : (
        <>
          <section className="integration-section" aria-labelledby="webhook-destinations-title">
            <div className="section-heading-row">
              <div>
                <h2 id="webhook-destinations-title">Webhook destinations</h2>
                <p>{integrations.length} configured · signed delivery retries transient failures</p>
              </div>
              <span className="badge badge-success">Available</span>
            </div>

            {integrations.length === 0 ? (
              <div className="empty-state integration-empty">
                <span className="empty-state-icon"><Webhook size={22} /></span>
                <h3>No webhook destinations</h3>
                <p>Connect a public HTTPS endpoint to receive signed call-completed events.</p>
                {canManage && <button className="btn btn-primary" onClick={openCreate}><Plus size={14} /> Add webhook</button>}
              </div>
            ) : (
              <div className="integration-list">
                {integrations.map((integration) => {
                  const configuredEvents = configEvents(integration);
                  return (
                    <article className="integration-row" key={integration.id}>
                      <span className="integration-row-icon"><Webhook size={17} /></span>
                      <div className="integration-row-main">
                        <div className="integration-row-title">
                          <h3>{integration.name}</h3>
                          <span className={`badge ${integration.is_active ? 'badge-success' : 'badge-neutral'}`}>
                            {integration.is_active ? 'Active' : 'Inactive'}
                          </span>
                        </div>
                        <p className="integration-url" title={configString(integration, 'url')}>
                          {configString(integration, 'url') || 'URL unavailable'}
                        </p>
                        <div className="integration-metadata">
                          <span><ShieldCheck size={12} /> Signed secret {integration.secret_fields.includes('signing_secret') ? 'configured' : 'missing'}</span>
                          <span>{configuredEvents.length} event{configuredEvents.length === 1 ? '' : 's'}</span>
                        </div>
                      </div>
                      {canManage && (
                        <div className="integration-row-actions">
                          <button className="btn btn-secondary btn-sm" onClick={() => openEdit(integration)} disabled={Boolean(working)}>
                            <Pencil size={12} /> Edit
                          </button>
                          <button
                            className="btn btn-danger btn-sm"
                            onClick={() => deleteIntegration(integration)}
                            disabled={Boolean(working)}
                          >
                            {working === `delete-${integration.id}` ? <Loader2 className="spin" size={12} /> : <Trash2 size={12} />}
                            Delete
                          </button>
                        </div>
                      )}
                    </article>
                  );
                })}
              </div>
            )}
          </section>

          <section className="integration-section planned-integrations" aria-labelledby="planned-integrations-title">
            <div className="section-heading-row">
              <div>
                <h2 id="planned-integrations-title">Planned connectors</h2>
                <p>Roadmap visibility only; these connectors are not available in this release.</p>
              </div>
              <span className="badge badge-info">Roadmap</span>
            </div>
            <div className="planned-integration-grid">
              {PLANNED_INTEGRATIONS.map((integration) => (
                <article className="planned-integration-card" key={integration.name}>
                  <div>
                    <h3>{integration.name}</h3>
                    <span className="badge badge-neutral">Coming soon</span>
                  </div>
                  <p>{integration.description}</p>
                </article>
              ))}
            </div>
          </section>
        </>
      )}
    </Layout>
  );
}
