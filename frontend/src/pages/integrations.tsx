import { FormEvent, Fragment, useEffect, useState } from 'react';
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
import { api, Integration, IntegrationDelivery } from '@/lib/api';
import {
  webhookReplayAvailability,
  webhookUndeliveredResultLabel,
} from '@/lib/webhook-delivery-actions.cjs';

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

function deliveryBadge(status: IntegrationDelivery['status']) {
  if (status === 'sent') return 'badge-success';
  if (status === 'failed') return 'badge-danger';
  return 'badge-warning';
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
  const [deliveries, setDeliveries] = useState<Record<string, IntegrationDelivery[]>>({});
  const [deliveryErrors, setDeliveryErrors] = useState<Record<string, string>>({});
  const [expandedIntegrationId, setExpandedIntegrationId] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    Promise.all([api.listIntegrations(), api.getMe()])
      .then(([items, user]) => {
        if (!active) return;
        const webhookIntegrations = items.filter((item) => item.integration_type === 'webhook');
        setIntegrations(webhookIntegrations);
        setCanManage(user.role === 'owner' || user.role === 'admin');
        setLoadError('');
        void Promise.allSettled(webhookIntegrations.map((integration) => (
          api.listIntegrationDeliveries(integration.id)
        ))).then((results) => {
          if (!active) return;
          const nextDeliveries: Record<string, IntegrationDelivery[]> = {};
          const nextErrors: Record<string, string> = {};
          results.forEach((result, index) => {
            const integrationId = webhookIntegrations[index].id;
            if (result.status === 'fulfilled') nextDeliveries[integrationId] = result.value;
            else nextErrors[integrationId] = messageFrom(result.reason, 'Delivery history could not be loaded.');
          });
          setDeliveries(nextDeliveries);
          setDeliveryErrors(nextErrors);
        });
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
    setDeliveries({});
    setDeliveryErrors({});
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
        setNotice({ type: 'success', text: `${updated.name} was saved. No test event was sent.` });
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
          setDeliveries((current) => ({ ...current, [staged.id]: [] }));
          setNotice({
            type: 'error',
            text: `${staged.name} was staged with no event subscriptions, but final setup failed: ${messageFrom(statusError, 'status update failed')}`,
          });
          closeForm();
          return;
        }
        setIntegrations((current) => [created, ...current]);
        setDeliveries((current) => ({ ...current, [created.id]: [] }));
        setNotice({
          type: 'success',
          text: `${created.name} was created${created.is_active ? ' as an active destination' : ' as inactive'}. No test event was sent.`,
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
      setDeliveries((current) => {
        const next = { ...current };
        delete next[integration.id];
        return next;
      });
      if (editingId === integration.id) closeForm();
      setNotice({ type: 'success', text: `${integration.name} was deleted.` });
    } catch (error) {
      setNotice({ type: 'error', text: messageFrom(error, 'Could not delete the destination.') });
    } finally {
      setWorking('');
    }
  };

  const refreshDeliveries = async (integration: Integration) => {
    setWorking(`deliveries-${integration.id}`);
    setDeliveryErrors((current) => ({ ...current, [integration.id]: '' }));
    try {
      const items = await api.listIntegrationDeliveries(integration.id);
      setDeliveries((current) => ({ ...current, [integration.id]: items }));
    } catch (error) {
      setDeliveryErrors((current) => ({
        ...current,
        [integration.id]: messageFrom(error, 'Delivery history could not be loaded.'),
      }));
    } finally {
      setWorking('');
    }
  };

  const testDestination = async (integration: Integration) => {
    setWorking(`test-${integration.id}`);
    setNotice(null);
    try {
      const delivery = await api.testIntegration(integration.id);
      setDeliveries((current) => ({
        ...current,
        [integration.id]: [delivery, ...(current[integration.id] ?? []).filter((item) => item.id !== delivery.id)],
      }));
      setExpandedIntegrationId(integration.id);
      setNotice({ type: 'success', text: `A signed test event for ${integration.name} was queued. Refresh the delivery log to confirm the result.` });
    } catch (error) {
      setNotice({ type: 'error', text: messageFrom(error, 'Could not queue the test event.') });
    } finally {
      setWorking('');
    }
  };

  const replayDelivery = async (integration: Integration, delivery: IntegrationDelivery) => {
    setWorking(`replay-${delivery.id}`);
    setNotice(null);
    try {
      const updated = await api.replayIntegrationDelivery(integration.id, delivery.id);
      setDeliveries((current) => ({
        ...current,
        [integration.id]: (current[integration.id] ?? []).map((item) => item.id === updated.id ? updated : item),
      }));
      setNotice({ type: 'success', text: `${delivery.event_type} was queued again with the same event ID.` });
    } catch (error) {
      setNotice({ type: 'error', text: messageFrom(error, 'Could not replay the failed delivery.') });
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
                <p>{integrations.length} configured · the server signs requests and retries eligible transient failures</p>
              </div>
              <span className="badge badge-neutral">Configuration</span>
            </div>

            <div className="integration-access-note" role="note">
              <CircleAlert size={15} />
              <span>Configured or active does not mean verified. Queue a signed test, then confirm its final status in the delivery log. Only failed deliveries can be replayed.</span>
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
                  const integrationDeliveries = deliveries[integration.id];
                  const latestDelivery = integrationDeliveries?.[0];
                  const isExpanded = expandedIntegrationId === integration.id;
                  return (
                    <Fragment key={integration.id}>
                      <article className="integration-row">
                        <span className="integration-row-icon"><Webhook size={17} /></span>
                        <div className="integration-row-main">
                          <div className="integration-row-title">
                            <h3>{integration.name}</h3>
                            <span className={`badge ${integration.is_active ? 'badge-success' : 'badge-neutral'}`}>
                              {integration.is_active ? 'Active config' : 'Inactive'}
                            </span>
                            {latestDelivery && (
                              <span className={`badge ${deliveryBadge(latestDelivery.status)}`}>Latest delivery: {latestDelivery.status}</span>
                            )}
                          </div>
                          <p className="integration-url" title={configString(integration, 'url')}>
                            {configString(integration, 'url') || 'URL unavailable'}
                          </p>
                          <div className="integration-metadata">
                            <span><ShieldCheck size={12} /> Signed secret {integration.secret_fields.includes('signing_secret') ? 'configured' : 'missing'}</span>
                            <span>{configuredEvents.length} event{configuredEvents.length === 1 ? '' : 's'}</span>
                            <span>{latestDelivery ? `Latest event ${new Date(latestDelivery.created_at).toLocaleString()}` : deliveryErrors[integration.id] ? 'Delivery status unavailable' : integrationDeliveries ? 'No delivery attempts recorded' : 'Loading delivery status…'}</span>
                          </div>
                        </div>
                        <div className="integration-row-actions">
                          <button
                            className="btn btn-secondary btn-sm"
                            type="button"
                            aria-expanded={isExpanded}
                            aria-controls={`integration-deliveries-${integration.id}`}
                            onClick={() => setExpandedIntegrationId(isExpanded ? null : integration.id)}
                          >
                            Delivery log
                          </button>
                          {canManage && (
                            <button className="btn btn-secondary btn-sm" type="button" onClick={() => void testDestination(integration)} disabled={Boolean(working) || !integration.is_active} title={!integration.is_active ? 'Activate the destination before testing.' : undefined}>
                              {working === `test-${integration.id}` ? <Loader2 className="spin" size={12} /> : <Webhook size={12} />} Test
                            </button>
                          )}
                          {canManage && (
                            <button className="btn btn-secondary btn-sm" onClick={() => openEdit(integration)} disabled={Boolean(working)}>
                              <Pencil size={12} /> Edit
                            </button>
                          )}
                          {canManage && (
                            <button className="btn btn-danger btn-sm" onClick={() => deleteIntegration(integration)} disabled={Boolean(working)}>
                              {working === `delete-${integration.id}` ? <Loader2 className="spin" size={12} /> : <Trash2 size={12} />} Delete
                            </button>
                          )}
                        </div>
                      </article>

                      {isExpanded && (
                        <section id={`integration-deliveries-${integration.id}`} className="card" aria-label={`${integration.name} delivery log`}>
                          <div className="card-title">
                            <div><h3>Recent delivery log</h3><p>Safe metadata only; payloads and credentials are not returned.</p></div>
                            <button className="btn btn-secondary btn-sm" type="button" onClick={() => void refreshDeliveries(integration)} disabled={Boolean(working)}>
                              {working === `deliveries-${integration.id}` ? <Loader2 className="spin" size={12} /> : <RotateCw size={12} />} Refresh
                            </button>
                          </div>
                          {deliveryErrors[integration.id] ? (
                            <div className="provider-alert provider-alert-error" role="alert">
                              <CircleAlert size={15} /><span>{deliveryErrors[integration.id]}</span>
                            </div>
                          ) : integrationDeliveries === undefined ? (
                            <div className="page-loading" role="status"><Loader2 className="spin" size={14} /> Loading delivery history…</div>
                          ) : integrationDeliveries.length === 0 ? (
                            <div className="empty-state"><h3>No deliveries recorded</h3><p>Queue a test event or wait for a subscribed call event.</p></div>
                          ) : (
                            <div className="table-container" role="region" aria-label={`${integration.name} recent webhook deliveries`} tabIndex={0}>
                              <table>
                                <caption className="visually-hidden">Recent webhook delivery attempts for {integration.name}</caption>
                                <thead><tr><th>Event</th><th>Status</th><th>Attempts</th><th>Created</th><th>Result</th><th><span className="visually-hidden">Actions</span></th></tr></thead>
                                <tbody>{integrationDeliveries.map((delivery) => {
                                  const replay = webhookReplayAvailability(
                                    integration.is_active,
                                    delivery.status,
                                  );
                                  const replayReasonId = `delivery-replay-reason-${delivery.id}`;
                                  return (
                                    <tr key={delivery.id}>
                                      <td><strong>{delivery.event_type}</strong><span className="settings-secondary-value">ID: {delivery.id}</span></td>
                                      <td><span className={`badge ${deliveryBadge(delivery.status)}`}>{delivery.status}</span></td>
                                      <td>{delivery.attempts}</td>
                                      <td>{new Date(delivery.created_at).toLocaleString()}</td>
                                      <td>{delivery.delivered_at ? `Delivered ${new Date(delivery.delivered_at).toLocaleString()}` : webhookUndeliveredResultLabel(delivery.last_error)}</td>
                                      <td>
                                        {canManage && delivery.status === 'failed' ? (
                                          <>
                                            <button
                                              className="btn btn-secondary btn-sm"
                                              type="button"
                                              onClick={() => void replayDelivery(integration, delivery)}
                                              disabled={Boolean(working) || !replay.enabled}
                                              aria-describedby={!replay.enabled ? replayReasonId : undefined}
                                            >
                                              {working === `replay-${delivery.id}` ? <Loader2 className="spin" size={12} /> : <RotateCw size={12} />} Replay
                                            </button>
                                            {!replay.enabled && (
                                              <span id={replayReasonId} className="settings-secondary-value">
                                                {replay.reason}
                                              </span>
                                            )}
                                          </>
                                        ) : <span className="table-muted">—</span>}
                                      </td>
                                    </tr>
                                  );
                                })}</tbody>
                              </table>
                            </div>
                          )}
                        </section>
                      )}
                    </Fragment>
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
