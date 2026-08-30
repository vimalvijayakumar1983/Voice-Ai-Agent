import Head from 'next/head';
import type { FormEvent } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  Check,
  Clipboard,
  Clock3,
  KeyRound,
  MailPlus,
  Radio,
  RefreshCw,
  ShieldCheck,
  UserCog,
  Users,
  X,
} from 'lucide-react';
import Layout from '@/components/Layout';
import {
  api,
  type AuditEvent,
  type CurrentUser,
  type ProviderStatus,
  type SipCredentialStatus,
  type WorkspaceApiKey,
  type WorkspaceInvitation,
  type WorkspaceRole,
} from '@/lib/api';

type AssignableRole = Exclude<WorkspaceRole, 'owner'>;
type SecretRevealState = {
  title: string;
  label: string;
  value: string;
  message: string;
  returnFocusId: string;
};

const DATE_FORMATTER = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'medium',
  timeStyle: 'short',
});

const ROLE_DESCRIPTIONS: Record<WorkspaceRole, string> = {
  owner: 'Full workspace and security control',
  admin: 'Manage members, invitations, and API keys',
  member: 'Build and operate voice agents',
  viewer: 'Read-only operational access',
};

function formatDate(value: string | null) {
  return value ? DATE_FORMATTER.format(new Date(value)) : 'Never';
}

function formatAction(action: string) {
  return action
    .replaceAll('.', ' ')
    .replaceAll('_', ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function describeEvent(event: AuditEvent) {
  const name = typeof event.details.name === 'string' ? event.details.name : null;
  const email = typeof event.details.email === 'string' ? event.details.email : null;
  const role = typeof event.details.role === 'string' ? event.details.role : null;
  if (email && role) return `${email} · ${role}`;
  if (email) return email;
  if (name) return name;
  return event.resource_type.replaceAll('_', ' ');
}

function SecretReveal({
  secret,
  copied,
  onCopy,
  onDismiss,
}: {
  secret: SecretRevealState;
  copied: boolean;
  onCopy: () => void;
  onDismiss: () => void;
}) {
  const secretInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    secretInput.current?.focus();
    secretInput.current?.select();
  }, []);

  return (
    <section
      className="secret-reveal"
      aria-labelledby="secret-reveal-title"
      aria-describedby="secret-reveal-message"
      aria-live="assertive"
      aria-atomic="true"
      role="region"
    >
      <div className="secret-reveal-icon"><ShieldCheck size={18} aria-hidden="true" /></div>
      <div className="secret-reveal-content">
        <div className="secret-reveal-heading">
          <div>
            <span className="page-kicker">Shown once</span>
            <h3 id="secret-reveal-title">{secret.title}</h3>
          </div>
          <button type="button" className="icon-button" onClick={onDismiss} aria-label="Dismiss secret">
            <X size={15} />
          </button>
        </div>
        <p id="secret-reveal-message">{secret.message}</p>
        <label htmlFor="one-time-secret">{secret.label}</label>
        <div className="secret-copy-row">
          <input
            id="one-time-secret"
            ref={secretInput}
            value={secret.value}
            readOnly
            aria-describedby="secret-reveal-message"
            onFocus={(event) => event.currentTarget.select()}
          />
          <button type="button" className="btn btn-primary" onClick={onCopy}>
            {copied ? <Check size={14} /> : <Clipboard size={14} />}
            {copied ? 'Copied' : 'Copy'}
          </button>
          <span className="visually-hidden" role="status" aria-live="polite">
            {copied ? 'Secret copied to the clipboard.' : ''}
          </span>
        </div>
      </div>
    </section>
  );
}

export default function Settings() {
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [members, setMembers] = useState<CurrentUser[]>([]);
  const [invitations, setInvitations] = useState<WorkspaceInvitation[]>([]);
  const [apiKeys, setApiKeys] = useState<WorkspaceApiKey[]>([]);
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
  const [providerStatus, setProviderStatus] = useState<ProviderStatus | null>(null);
  const [sipStatus, setSipStatus] = useState<SipCredentialStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [notice, setNotice] = useState('');
  const [busyAction, setBusyAction] = useState('');
  const [reloadKey, setReloadKey] = useState(0);
  const [secret, setSecret] = useState<SecretRevealState | null>(null);
  const [copied, setCopied] = useState(false);
  const [inviteForm, setInviteForm] = useState({
    full_name: '',
    email: '',
    role: 'member' as AssignableRole,
  });
  const [apiKeyName, setApiKeyName] = useState('');
  const [sarvamApiKey, setSarvamApiKey] = useState('');
  const [sipForm, setSipForm] = useState({
    sip_uri: '',
    username: '',
    password: '',
    inbound_number: '',
    livekit_url: '',
    livekit_api_key: '',
    livekit_api_secret: '',
  });

  useEffect(() => {
    let active = true;
    async function loadWorkspace() {
      setLoading(true);
      setLoadError('');
      try {
        const user = await api.getMe();
        if (!active) return;
        setCurrentUser(user);
        if (user.role !== 'owner' && user.role !== 'admin') {
          setLoading(false);
          return;
        }
        const [nextMembers, nextInvitations, nextApiKeys, nextAuditEvents, nextProviderStatus, nextSipStatus] = await Promise.all([
          api.listWorkspaceUsers(),
          api.listInvitations(),
          api.listApiKeys(),
          api.listAuditEvents(12),
          api.getProviderStatus(),
          api.getSipCredentialStatus(),
        ]);
        if (!active) return;
        setMembers(nextMembers);
        setInvitations(nextInvitations);
        setApiKeys(nextApiKeys);
        setAuditEvents(nextAuditEvents);
        setProviderStatus(nextProviderStatus);
        setSipStatus(nextSipStatus);
      } catch (caught: unknown) {
        if (active) {
          setLoadError(caught instanceof Error ? caught.message : 'Workspace settings could not be loaded.');
        }
      } finally {
        if (active) setLoading(false);
      }
    }
    void loadWorkspace();
    return () => {
      active = false;
    };
  }, [reloadKey]);

  const userById = useMemo(
    () => new Map(members.map((member) => [member.id, member])),
    [members],
  );
  const isOwner = currentUser?.role === 'owner';
  const canAdminister = isOwner || currentUser?.role === 'admin';
  const pendingInvitations = invitations.filter((invitation) => invitation.status === 'pending').length;
  const activeKeys = apiKeys.filter((key) => key.is_active).length;

  const resetMessages = () => {
    setActionError('');
    setNotice('');
    setCopied(false);
  };

  const refreshAudit = async () => {
    setAuditEvents(await api.listAuditEvents(12));
  };

  const handleInvite = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    resetMessages();
    setBusyAction('invite');
    try {
      const created = await api.createInvitation(inviteForm);
      // Keep the one-time token in the URL fragment so it is never sent in
      // HTTP requests, CDN logs, analytics, or Referer headers.
      const inviteLink = `${window.location.origin}/accept-invite#token=${encodeURIComponent(created.token)}`;
      setSecret({
        title: 'Invitation created',
        label: 'Invitation link',
        value: inviteLink,
        message: 'Share this link securely. It expires in seven days and cannot be recovered later.',
        returnFocusId: 'invite-email',
      });
      setInviteForm({ full_name: '', email: '', role: 'member' });
      const [nextInvitations, nextAuditEvents] = await Promise.all([
        api.listInvitations(),
        api.listAuditEvents(12),
      ]);
      setInvitations(nextInvitations);
      setAuditEvents(nextAuditEvents);
      setNotice(`Invitation ready for ${created.email}.`);
    } catch (caught: unknown) {
      setActionError(caught instanceof Error ? caught.message : 'Invitation could not be created.');
    } finally {
      setBusyAction('');
    }
  };

  const handleRevokeInvitation = async (invitation: WorkspaceInvitation) => {
    resetMessages();
    if (!window.confirm(`Revoke the invitation for ${invitation.email}?`)) return;
    setBusyAction(`invitation-${invitation.id}`);
    try {
      await api.revokeInvitation(invitation.id);
      const [nextInvitations, nextAuditEvents] = await Promise.all([
        api.listInvitations(),
        api.listAuditEvents(12),
      ]);
      setInvitations(nextInvitations);
      setAuditEvents(nextAuditEvents);
      setNotice(`Invitation for ${invitation.email} revoked.`);
    } catch (caught: unknown) {
      setActionError(caught instanceof Error ? caught.message : 'Invitation could not be revoked.');
    } finally {
      setBusyAction('');
    }
  };

  const handleMemberUpdate = async (
    member: CurrentUser,
    changes: { role?: AssignableRole; is_active?: boolean },
  ) => {
    resetMessages();
    setBusyAction(`member-${member.id}`);
    try {
      const updated = await api.updateWorkspaceUser(member.id, changes);
      setMembers((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      await refreshAudit();
      setNotice(`${updated.full_name}'s access was updated.`);
    } catch (caught: unknown) {
      setActionError(caught instanceof Error ? caught.message : 'Member access could not be updated.');
    } finally {
      setBusyAction('');
    }
  };

  const handleCreateApiKey = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    resetMessages();
    setBusyAction('api-key');
    try {
      const created = await api.createApiKey(apiKeyName.trim());
      setSecret({
        title: 'API key created',
        label: 'Secret API key',
        value: created.key,
        message: 'Copy this key into your secret manager now. It will never be shown again.',
        returnFocusId: 'api-key-name',
      });
      setApiKeyName('');
      const [nextApiKeys, nextAuditEvents] = await Promise.all([
        api.listApiKeys(),
        api.listAuditEvents(12),
      ]);
      setApiKeys(nextApiKeys);
      setAuditEvents(nextAuditEvents);
      setNotice(`${created.name} is ready to use.`);
    } catch (caught: unknown) {
      setActionError(caught instanceof Error ? caught.message : 'API key could not be created.');
    } finally {
      setBusyAction('');
    }
  };

  const handleRevokeApiKey = async (key: WorkspaceApiKey) => {
    resetMessages();
    if (!window.confirm(`Revoke the API key “${key.name}”? Applications using it will stop working.`)) return;
    setBusyAction(`key-${key.id}`);
    try {
      await api.revokeApiKey(key.id);
      const [nextKeys, nextAuditEvents] = await Promise.all([
        api.listApiKeys(),
        api.listAuditEvents(12),
      ]);
      setApiKeys(nextKeys);
      setAuditEvents(nextAuditEvents);
      setNotice(`${key.name} was revoked.`);
    } catch (caught: unknown) {
      setActionError(caught instanceof Error ? caught.message : 'API key could not be revoked.');
    } finally {
      setBusyAction('');
    }
  };

  const handleSaveSarvamKey = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    resetMessages();
    setBusyAction('sarvam-key');
    try {
      await api.saveSarvamCredential(sarvamApiKey.trim());
      setSarvamApiKey('');
      const [nextProviderStatus, nextAuditEvents] = await Promise.all([
        api.getProviderStatus(),
        api.listAuditEvents(12),
      ]);
      setProviderStatus(nextProviderStatus);
      setAuditEvents(nextAuditEvents);
      setNotice('Sarvam AI credential saved securely. Sarvam voices are now available in the agent builder.');
    } catch (caught: unknown) {
      setActionError(caught instanceof Error ? caught.message : 'Sarvam credential could not be saved.');
    } finally {
      setBusyAction('');
    }
  };

  const handleDeleteSarvamKey = async () => {
    resetMessages();
    if (!window.confirm('Remove the workspace Sarvam API key? Sarvam previews and new Sarvam calls will stop working.')) return;
    setBusyAction('sarvam-delete');
    try {
      await api.deleteSarvamCredential();
      const [nextProviderStatus, nextAuditEvents] = await Promise.all([
        api.getProviderStatus(),
        api.listAuditEvents(12),
      ]);
      setProviderStatus(nextProviderStatus);
      setAuditEvents(nextAuditEvents);
      setNotice('Workspace Sarvam credential removed.');
    } catch (caught: unknown) {
      setActionError(caught instanceof Error ? caught.message : 'Sarvam credential could not be removed.');
    } finally {
      setBusyAction('');
    }
  };

  const handleSaveSipCredential = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    resetMessages();
    setBusyAction('sip-key');
    try {
      const next = await api.saveSipCredential(sipForm);
      setSipStatus(next);
      setSipForm({
        sip_uri: '', username: '', password: '', inbound_number: '',
        livekit_url: '', livekit_api_key: '', livekit_api_secret: '',
      });
      await refreshAudit();
      setNotice('Etisalat SIP and LiveKit credentials saved securely. Assign the number in an agent runtime before activation.');
    } catch (caught: unknown) {
      setActionError(caught instanceof Error ? caught.message : 'SIP credentials could not be saved.');
    } finally {
      setBusyAction('');
    }
  };

  const handleDeleteSipCredential = async () => {
    resetMessages();
    if (!window.confirm('Remove the Etisalat SIP and LiveKit credentials? SIP runtimes will fail readiness immediately.')) return;
    setBusyAction('sip-delete');
    try {
      setSipStatus(await api.deleteSipCredential());
      await refreshAudit();
      setNotice('Etisalat SIP and LiveKit credentials removed.');
    } catch (caught: unknown) {
      setActionError(caught instanceof Error ? caught.message : 'SIP credentials could not be removed.');
    } finally {
      setBusyAction('');
    }
  };

  const copySecret = async () => {
    if (!secret) return;
    try {
      await navigator.clipboard.writeText(secret.value);
      setCopied(true);
    } catch {
      setActionError('Copy was blocked by the browser. Select the value and copy it manually.');
    }
  };

  const dismissSecret = () => {
    const returnFocusId = secret?.returnFocusId;
    setSecret(null);
    setCopied(false);
    if (returnFocusId) {
      window.requestAnimationFrame(() => document.getElementById(returnFocusId)?.focus());
    }
  };

  return (
    <Layout>
      <Head><title>Workspace settings | VAV Voice AI</title></Head>
      <div className="page-header">
        <div>
          <span className="page-kicker">Workspace control</span>
          <h1>Settings</h1>
          <p className="page-subtitle">Manage team access, secure invitations, credentials, and administrative history.</p>
        </div>
        <div className="header-actions">
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => setReloadKey((value) => value + 1)}
            disabled={loading}
          >
            <RefreshCw size={14} /> Refresh
          </button>
        </div>
      </div>

      {loading ? (
        <div className="settings-loading" role="status" aria-live="polite">
          <RefreshCw size={20} className="spin" aria-hidden="true" />
          <strong>Loading workspace controls</strong>
          <span>Checking members, invitations, and credentials…</span>
        </div>
      ) : loadError ? (
        <div className="card settings-error" role="alert">
          <div><strong>Settings unavailable</strong><p>{loadError}</p></div>
          <button type="button" className="btn btn-secondary" onClick={() => setReloadKey((value) => value + 1)}>Try again</button>
        </div>
      ) : !canAdminister ? (
        <div className="card settings-access-state">
          <div className="empty-state-icon"><ShieldCheck size={24} /></div>
          <h2>Administrator access required</h2>
          <p>Your {currentUser?.role} role does not include workspace administration. Ask an owner or administrator to make changes.</p>
        </div>
      ) : (
        <>
          <div className="settings-summary" aria-label="Workspace administration summary">
            <div className="settings-summary-card"><Users size={17} /><div><strong>{members.length}</strong><span>Team members</span></div></div>
            <div className="settings-summary-card"><MailPlus size={17} /><div><strong>{pendingInvitations}</strong><span>Pending invitations</span></div></div>
            <div className="settings-summary-card"><KeyRound size={17} /><div><strong>{activeKeys}</strong><span>Active API keys</span></div></div>
            <div className="settings-summary-card"><ShieldCheck size={17} /><div><strong>{currentUser?.role}</strong><span>Your access level</span></div></div>
          </div>

          {actionError ? <div className="auth-error settings-message" role="alert">{actionError}</div> : null}
          {notice ? <div className="settings-notice settings-message" role="status">{notice}</div> : null}
          {secret ? (
            <SecretReveal
              secret={secret}
              copied={copied}
              onCopy={() => void copySecret()}
              onDismiss={dismissSecret}
            />
          ) : null}

          <section className="settings-section" aria-labelledby="team-heading">
            <div className="settings-section-heading">
              <div className="settings-section-icon"><Users size={18} /></div>
              <div><h2 id="team-heading">Team and role access</h2><p>Owners control roles and account activation. Administrators have a read-only view here.</p></div>
              <span className="badge badge-info">{members.length} members</span>
            </div>
            <div className="table-container settings-table">
              <table>
                <caption className="visually-hidden">Workspace team members and access levels</caption>
                <thead><tr><th>Member</th><th>Role</th><th>Access</th><th>Permissions</th><th><span className="visually-hidden">Actions</span></th></tr></thead>
                <tbody>
                  {members.map((member) => {
                    const locked = !isOwner || member.role === 'owner' || member.id === currentUser?.id;
                    return (
                      <tr key={member.id}>
                        <td><strong className="settings-primary-value">{member.full_name}</strong><span className="settings-secondary-value">{member.email}</span></td>
                        <td>
                          {locked ? <span className={`badge ${member.role === 'owner' ? 'badge-info' : 'badge-neutral'}`}>{member.role}</span> : (
                            <select
                              className="compact-select"
                              aria-label={`Role for ${member.full_name}`}
                              value={member.role}
                              disabled={busyAction === `member-${member.id}`}
                              onChange={(event) => void handleMemberUpdate(member, { role: event.target.value as AssignableRole })}
                            >
                              <option value="admin">Admin</option>
                              <option value="member">Member</option>
                              <option value="viewer">Viewer</option>
                            </select>
                          )}
                        </td>
                        <td><span className={`badge ${member.is_active ? 'badge-success' : 'badge-danger'}`}>{member.is_active ? 'Active' : 'Disabled'}</span></td>
                        <td className="table-muted">{ROLE_DESCRIPTIONS[member.role]}</td>
                        <td className="settings-action-cell">
                          {!locked ? (
                            <button
                              type="button"
                              className={`btn btn-sm ${member.is_active ? 'btn-danger' : 'btn-secondary'}`}
                              disabled={busyAction === `member-${member.id}`}
                              onClick={() => void handleMemberUpdate(member, { is_active: !member.is_active })}
                            >{member.is_active ? 'Disable' : 'Restore'}</button>
                          ) : <span className="table-muted">Protected</span>}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>

          <section className="settings-section" aria-labelledby="invitations-heading">
            <div className="settings-section-heading">
              <div className="settings-section-icon"><MailPlus size={18} /></div>
              <div><h2 id="invitations-heading">Workspace invitations</h2><p>Create a seven-day, single-use invitation link. The secret link is shown only once.</p></div>
            </div>
            <form className="invite-form" onSubmit={handleInvite}>
              <div className="form-group"><label htmlFor="invite-name">Full name</label><input id="invite-name" autoComplete="off" value={inviteForm.full_name} onChange={(event) => setInviteForm((form) => ({ ...form, full_name: event.target.value }))} required /></div>
              <div className="form-group"><label htmlFor="invite-email">Work email</label><input id="invite-email" type="email" inputMode="email" autoComplete="off" value={inviteForm.email} onChange={(event) => setInviteForm((form) => ({ ...form, email: event.target.value }))} required /></div>
              <div className="form-group"><label htmlFor="invite-role">Role</label><select id="invite-role" value={inviteForm.role} onChange={(event) => setInviteForm((form) => ({ ...form, role: event.target.value as AssignableRole }))}><option value="member">Member</option><option value="viewer">Viewer</option>{isOwner ? <option value="admin">Admin</option> : null}</select></div>
              <button type="submit" className="btn btn-primary invite-submit" disabled={busyAction === 'invite'}><MailPlus size={14} /> {busyAction === 'invite' ? 'Creating…' : 'Create invitation'}</button>
            </form>
            {invitations.length ? (
              <div className="table-container settings-table settings-subtable">
                <table>
                  <caption className="visually-hidden">Workspace invitation history</caption>
                  <thead><tr><th>Invitee</th><th>Role</th><th>Status</th><th>Expires</th><th><span className="visually-hidden">Actions</span></th></tr></thead>
                  <tbody>{invitations.map((invitation) => (
                    <tr key={invitation.id}>
                      <td><strong className="settings-primary-value">{invitation.full_name}</strong><span className="settings-secondary-value">{invitation.email}</span></td>
                      <td><span className="badge badge-neutral">{invitation.role}</span></td>
                      <td><span className={`badge ${invitation.status === 'pending' ? 'badge-warning' : invitation.status === 'accepted' ? 'badge-success' : 'badge-neutral'}`}>{invitation.status}</span></td>
                      <td className="table-muted">{formatDate(invitation.expires_at)}</td>
                      <td className="settings-action-cell">{invitation.status === 'pending' ? <button type="button" className="btn btn-danger btn-sm" disabled={busyAction === `invitation-${invitation.id}`} onClick={() => void handleRevokeInvitation(invitation)}>Revoke</button> : <span className="table-muted">—</span>}</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            ) : <div className="settings-empty-row"><MailPlus size={18} /><span>No invitations have been created.</span></div>}
          </section>

          <section className="settings-section" aria-labelledby="provider-credentials-heading">
            <div className="settings-section-heading">
              <div className="settings-section-icon"><Radio size={18} /></div>
              <div>
                <h2 id="provider-credentials-heading">Voice AI providers</h2>
                <p>Connect provider credentials for this workspace. Keys are encrypted on the server and are never returned to the browser.</p>
              </div>
              <span className={`badge ${providerStatus?.providers?.sarvam.configured ? 'badge-success' : 'badge-warning'}`}>
                Sarvam {providerStatus?.providers?.sarvam.configured ? 'connected' : 'not connected'}
              </span>
            </div>
            <form className="api-key-form" onSubmit={handleSaveSarvamKey}>
              <div className="form-group">
                <label htmlFor="sarvam-api-key">Sarvam API key</label>
                <input
                  id="sarvam-api-key"
                  type="password"
                  autoComplete="new-password"
                  placeholder={providerStatus?.providers?.sarvam.configured ? 'Enter a new key to rotate' : 'sk_…'}
                  value={sarvamApiKey}
                  onChange={(event) => setSarvamApiKey(event.target.value)}
                  minLength={20}
                  maxLength={512}
                  required
                />
                <p className="form-hint">The saved value is write-only. Entering another key rotates it immediately for this workspace.</p>
              </div>
              <button type="submit" className="btn btn-primary" disabled={busyAction === 'sarvam-key'}>
                <KeyRound size={14} /> {busyAction === 'sarvam-key' ? 'Saving…' : providerStatus?.providers?.sarvam.configured ? 'Rotate Sarvam key' : 'Connect Sarvam'}
              </button>
              {providerStatus?.providers?.sarvam.source === 'workspace' ? (
                <button type="button" className="btn btn-danger" disabled={busyAction === 'sarvam-delete'} onClick={() => void handleDeleteSarvamKey()}>
                  {busyAction === 'sarvam-delete' ? 'Removing…' : 'Remove workspace key'}
                </button>
              ) : null}
            </form>
            <div className="settings-section-heading settings-subtable">
              <div className="settings-section-icon"><Radio size={18} /></div>
              <div>
                <h3>Etisalat SIP edge</h3>
                <p>Store the carrier trunk and LiveKit gateway credentials as write-only encrypted values.</p>
              </div>
              <span className={`badge ${sipStatus?.configured ? 'badge-success' : 'badge-warning'}`}>
                SIP {sipStatus?.configured ? 'connected' : 'not connected'}
              </span>
            </div>
            <form className="api-key-form" onSubmit={handleSaveSipCredential}>
              <div className="form-grid">
                <div className="form-group"><label htmlFor="sip-uri">Etisalat SIP URI</label><input id="sip-uri" placeholder="sip:trunk.example.ae" value={sipForm.sip_uri} onChange={(event) => setSipForm({ ...sipForm, sip_uri: event.target.value })} required /></div>
                <div className="form-group"><label htmlFor="sip-user">SIP username</label><input id="sip-user" autoComplete="off" value={sipForm.username} onChange={(event) => setSipForm({ ...sipForm, username: event.target.value })} required /></div>
                <div className="form-group"><label htmlFor="sip-password">SIP password</label><input id="sip-password" type="password" autoComplete="new-password" value={sipForm.password} onChange={(event) => setSipForm({ ...sipForm, password: event.target.value })} minLength={8} required /></div>
                <div className="form-group"><label htmlFor="sip-number">Inbound DID</label><input id="sip-number" placeholder="+971…" value={sipForm.inbound_number} onChange={(event) => setSipForm({ ...sipForm, inbound_number: event.target.value })} required /></div>
                <div className="form-group"><label htmlFor="livekit-url">LiveKit URL</label><input id="livekit-url" placeholder="wss://…livekit.cloud" value={sipForm.livekit_url} onChange={(event) => setSipForm({ ...sipForm, livekit_url: event.target.value })} required /></div>
                <div className="form-group"><label htmlFor="livekit-key">LiveKit API key</label><input id="livekit-key" type="password" autoComplete="new-password" value={sipForm.livekit_api_key} onChange={(event) => setSipForm({ ...sipForm, livekit_api_key: event.target.value })} minLength={8} required /></div>
                <div className="form-group"><label htmlFor="livekit-secret">LiveKit API secret</label><input id="livekit-secret" type="password" autoComplete="new-password" value={sipForm.livekit_api_secret} onChange={(event) => setSipForm({ ...sipForm, livekit_api_secret: event.target.value })} minLength={16} required /></div>
              </div>
              <button type="submit" className="btn btn-primary" disabled={busyAction === 'sip-key'}><KeyRound size={14} /> {busyAction === 'sip-key' ? 'Saving…' : sipStatus?.configured ? 'Rotate SIP credentials' : 'Connect SIP edge'}</button>
              {sipStatus?.configured ? <button type="button" className="btn btn-danger" disabled={busyAction === 'sip-delete'} onClick={() => void handleDeleteSipCredential()}>{busyAction === 'sip-delete' ? 'Removing…' : 'Remove SIP credentials'}</button> : null}
            </form>
          </section>

          <div className="settings-two-column">
            <section className="settings-section" aria-labelledby="api-keys-heading">
              <div className="settings-section-heading">
                <div className="settings-section-icon"><KeyRound size={18} /></div>
                <div><h2 id="api-keys-heading">API keys</h2><p>Read-only viewer credentials that expire after 90 days. Resource-scoped service accounts are not available.</p></div>
              </div>
              <form className="api-key-form" onSubmit={handleCreateApiKey}>
                <div className="form-group"><label htmlFor="api-key-name">Key name</label><input id="api-key-name" placeholder="Production backend" value={apiKeyName} onChange={(event) => setApiKeyName(event.target.value)} maxLength={255} required /></div>
                <button type="submit" className="btn btn-primary" disabled={busyAction === 'api-key'}><KeyRound size={14} /> {busyAction === 'api-key' ? 'Generating…' : 'Generate key'}</button>
              </form>
              <div className="credential-list">
                {apiKeys.length ? apiKeys.map((key) => (
                  <div className="credential-row" key={key.id}>
                    <div className="credential-icon"><KeyRound size={15} /></div>
                    <div><strong>{key.name}</strong><span>Expires {formatDate(key.expires_at)} · Last used {formatDate(key.last_used_at)}</span></div>
                    <span className={`badge ${key.is_active ? 'badge-success' : 'badge-neutral'}`}>{key.is_active ? 'Active' : 'Revoked'}</span>
                    {key.is_active ? <button type="button" className="btn btn-danger btn-sm" disabled={busyAction === `key-${key.id}`} onClick={() => void handleRevokeApiKey(key)}>Revoke</button> : null}
                  </div>
                )) : <div className="settings-empty-row"><KeyRound size={18} /><span>No API keys yet.</span></div>}
              </div>
            </section>

            <section className="settings-section" aria-labelledby="audit-heading">
              <div className="settings-section-heading">
                <div className="settings-section-icon"><Activity size={18} /></div>
                <div><h2 id="audit-heading">Recent audit events</h2><p>Recorded identity and administrative events. This is not a complete tamper-evident audit export.</p></div>
              </div>
              <ol className="audit-list">
                {auditEvents.length ? auditEvents.map((event) => {
                  const actor = event.actor_user_id ? userById.get(event.actor_user_id) : null;
                  return (
                    <li key={event.id}>
                      <div className="audit-marker"><UserCog size={14} /></div>
                      <div><strong>{formatAction(event.action)}</strong><span>{describeEvent(event)}</span><small>{actor?.full_name || 'System or former member'}</small></div>
                      <time dateTime={event.created_at}><Clock3 size={11} /> {formatDate(event.created_at)}</time>
                    </li>
                  );
                }) : <li className="settings-empty-row"><Activity size={18} /><span>No administrative activity yet.</span></li>}
              </ol>
            </section>
          </div>
        </>
      )}
    </Layout>
  );
}
