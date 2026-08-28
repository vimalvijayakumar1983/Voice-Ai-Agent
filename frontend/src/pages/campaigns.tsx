import { FormEvent, useEffect, useState } from 'react';
import Link from 'next/link';
import {
  CalendarClock,
  CheckCircle2,
  CircleAlert,
  Clock3,
  Pause,
  Play,
  Plus,
  RotateCw,
  Users,
  X,
} from 'lucide-react';
import Layout from '@/components/Layout';
import { isAgentCallReady } from '@/lib/agent-readiness.cjs';
import {
  api,
  Campaign,
  CampaignAttempt,
  CampaignAttemptReconciliation,
  CampaignContactInput,
  CurrentUser,
  VoiceAgent,
} from '@/lib/api';

interface CampaignForm {
  name: string;
  agent_id: string;
  timezone: string;
  calling_hours_start: string;
  calling_hours_end: string;
  scheduled_start: string;
  scheduled_end: string;
  max_concurrent_calls: number;
  retry_attempts: number;
  contacts: string;
}

interface PageNotice {
  type: 'success' | 'error';
  text: string;
}

interface ReconciliationForm {
  reason: string;
}

const INITIAL_RECONCILIATION: ReconciliationForm = {
  reason: '',
};

const E164_PATTERN = /^\+[1-9]\d{7,14}$/;
const TIME_PATTERN = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
const COMMON_TIMEZONES = [
  { value: 'UTC', label: 'UTC' },
  { value: 'Asia/Dubai', label: 'United Arab Emirates' },
  { value: 'Asia/Kolkata', label: 'India' },
  { value: 'Asia/Singapore', label: 'Singapore' },
  { value: 'Europe/London', label: 'United Kingdom' },
  { value: 'America/New_York', label: 'US Eastern' },
  { value: 'America/Los_Angeles', label: 'US Pacific' },
  { value: 'Australia/Sydney', label: 'Australia Eastern' },
];

function initialForm(timezone: string): CampaignForm {
  return {
    name: '',
    agent_id: '',
    timezone,
    calling_hours_start: '09:00',
    calling_hours_end: '17:00',
    scheduled_start: '',
    scheduled_end: '',
    max_concurrent_calls: 5,
    retry_attempts: 2,
    contacts: '',
  };
}

function messageFrom(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function parseContacts(value: string) {
  const contacts: CampaignContactInput[] = [];
  const errors: string[] = [];
  const seen = new Set<string>();
  const lines = value.split(/\r?\n/);

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index].trim();
    if (!line) continue;

    const [rawPhone, ...rawName] = line.split(',');
    const phone = rawPhone.trim();
    const name = rawName.join(',').trim();

    if (!E164_PATTERN.test(phone)) {
      errors.push(`Line ${index + 1}: use E.164 format, for example +971501234567.`);
      continue;
    }
    if (seen.has(phone)) {
      errors.push(`Line ${index + 1}: ${phone} is already in this campaign.`);
      continue;
    }
    if (name.length > 255) {
      errors.push(`Line ${index + 1}: the contact name must be 255 characters or fewer.`);
      continue;
    }

    seen.add(phone);
    contacts.push({ phone_number: phone, ...(name ? { name } : {}) });
  }

  if (contacts.length > 10_000) {
    errors.push('A campaign can contain at most 10,000 contacts.');
  }
  return { contacts, errors };
}

function isIanaTimezone(value: string) {
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: value }).format();
    return true;
  } catch {
    return false;
  }
}

function zonedParts(date: Date, timezone: string) {
  const formatter = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  });
  const values = new Map(formatter.formatToParts(date).map((part) => [part.type, part.value]));
  return {
    year: Number(values.get('year')),
    month: Number(values.get('month')),
    day: Number(values.get('day')),
    hour: Number(values.get('hour')),
    minute: Number(values.get('minute')),
    second: Number(values.get('second')),
  };
}

function localDateTimeToIso(value: string, timezone: string) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!match) throw new Error('Scheduled dates must include a date and time.');

  const [, year, month, day, hour, minute] = match;
  const desired = {
    year: Number(year),
    month: Number(month),
    day: Number(day),
    hour: Number(hour),
    minute: Number(minute),
  };
  const wallClockUtc = Date.UTC(
    desired.year,
    desired.month - 1,
    desired.day,
    desired.hour,
    desired.minute,
  );
  let instant = wallClockUtc;

  for (let pass = 0; pass < 2; pass += 1) {
    const parts = zonedParts(new Date(instant), timezone);
    const representedUtc = Date.UTC(
      parts.year,
      parts.month - 1,
      parts.day,
      parts.hour,
      parts.minute,
      parts.second,
    );
    instant = wallClockUtc - (representedUtc - instant);
  }

  const roundTrip = zonedParts(new Date(instant), timezone);
  if (
    roundTrip.year !== desired.year
    || roundTrip.month !== desired.month
    || roundTrip.day !== desired.day
    || roundTrip.hour !== desired.hour
    || roundTrip.minute !== desired.minute
  ) {
    throw new Error(`That local time does not exist in ${timezone}. Choose another time.`);
  }
  return new Date(instant).toISOString();
}

function formatSchedule(value: string | null, timezone: string) {
  if (!value) return null;
  try {
    return new Intl.DateTimeFormat(undefined, {
      timeZone: timezone,
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(new Date(value));
  } catch {
    return new Date(value).toLocaleString();
  }
}

function statusBadge(status: string) {
  const classes: Record<string, string> = {
    draft: 'badge-neutral',
    scheduled: 'badge-info',
    running: 'badge-success',
    paused: 'badge-warning',
    completed: 'badge-success',
    cancelled: 'badge-danger',
  };
  return classes[status] || 'badge-info';
}

export default function Campaigns() {
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);
  const [loadError, setLoadError] = useState('');
  const [showCreate, setShowCreate] = useState(false);
  const [browserTimezone, setBrowserTimezone] = useState('UTC');
  const [form, setForm] = useState<CampaignForm>(() => initialForm('UTC'));
  const [formError, setFormError] = useState('');
  const [creating, setCreating] = useState(false);
  const [working, setWorking] = useState('');
  const [pendingStart, setPendingStart] = useState<Campaign | null>(null);
  const [notice, setNotice] = useState<PageNotice | null>(null);
  const [reviewCampaign, setReviewCampaign] = useState<Campaign | null>(null);
  const [attempts, setAttempts] = useState<CampaignAttempt[]>([]);
  const [attemptsLoading, setAttemptsLoading] = useState(false);
  const [attemptsLoadFailed, setAttemptsLoadFailed] = useState(false);
  const [attemptError, setAttemptError] = useState('');
  const [selectedAttempt, setSelectedAttempt] = useState<CampaignAttempt | null>(null);
  const [reconciliation, setReconciliation] = useState<ReconciliationForm>(INITIAL_RECONCILIATION);
  const [reconciling, setReconciling] = useState(false);

  useEffect(() => {
    let active = true;
    Promise.allSettled([
      api.listCampaigns(),
      api.listAgents(),
      api.getMe(),
    ]).then(([campaignResult, agentResult, userResult]) => {
      if (!active) return;
      const errors: string[] = [];

      if (campaignResult.status === 'fulfilled') {
        setCampaigns(campaignResult.value);
      } else {
        errors.push(messageFrom(campaignResult.reason, 'Could not load campaigns.'));
      }
      if (agentResult.status === 'fulfilled') {
        setAgents(agentResult.value);
      } else {
        errors.push(messageFrom(agentResult.reason, 'Could not load agents.'));
      }
      if (userResult.status === 'fulfilled') {
        setCurrentUser(userResult.value);
      } else {
        errors.push(messageFrom(userResult.reason, 'Could not load your workspace role.'));
      }
      setLoadError(errors.join(' '));
      setLoading(false);
    });
    return () => {
      active = false;
    };
  }, [reloadKey]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      const detected = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
      setBrowserTimezone(detected);
      setForm((current) => (
        current.timezone === 'UTC' ? { ...current, timezone: detected } : current
      ));
    });
    return () => window.cancelAnimationFrame(frame);
  }, []);

  const retryLoad = () => {
    setLoading(true);
    setLoadError('');
    setReloadKey((current) => current + 1);
  };

  const canMutateCampaigns = Boolean(currentUser && currentUser.role !== 'viewer');
  const canReconcileAttempts = currentUser?.role === 'owner' || currentUser?.role === 'admin';
  const provisionedAgents = agents.filter(isAgentCallReady);
  const agentNames = new Map(agents.map((agent) => [agent.id, agent.name]));
  const contactLineCount = form.contacts.split(/\r?\n/).filter((line) => line.trim()).length;
  const timezoneOptions = COMMON_TIMEZONES.some((option) => option.value === browserTimezone)
    ? COMMON_TIMEZONES
    : [{ value: browserTimezone, label: 'Your browser timezone' }, ...COMMON_TIMEZONES];

  const openCreate = () => {
    setForm((current) => ({
      ...current,
      agent_id: current.agent_id || provisionedAgents[0]?.id || '',
    }));
    setFormError('');
    setNotice(null);
    setShowCreate(true);
  };

  const closeCreate = () => {
    setShowCreate(false);
    setFormError('');
  };

  const handleCreate = async (event: FormEvent) => {
    event.preventDefault();
    setFormError('');
    setNotice(null);

    const name = form.name.trim();
    if (name.length < 2) {
      setFormError('Campaign name must contain at least two characters.');
      return;
    }
    if (!provisionedAgents.some((agent) => agent.id === form.agent_id)) {
      setFormError('Choose an active agent that has been provisioned to Smallest.ai.');
      return;
    }
    const timezone = form.timezone.trim();
    if (!isIanaTimezone(timezone)) {
      setFormError('Enter a valid IANA timezone, such as Asia/Dubai or Asia/Kolkata.');
      return;
    }
    if (!TIME_PATTERN.test(form.calling_hours_start) || !TIME_PATTERN.test(form.calling_hours_end)) {
      setFormError('Choose both calling-window times.');
      return;
    }
    if (form.calling_hours_start === form.calling_hours_end) {
      setFormError('Calling-window start and end must be different.');
      return;
    }
    if (!Number.isInteger(form.max_concurrent_calls) || form.max_concurrent_calls < 1 || form.max_concurrent_calls > 100) {
      setFormError('Concurrent calls must be a whole number between 1 and 100.');
      return;
    }
    if (!Number.isInteger(form.retry_attempts) || form.retry_attempts < 0 || form.retry_attempts > 10) {
      setFormError('Retry attempts must be a whole number between 0 and 10.');
      return;
    }

    const parsed = parseContacts(form.contacts);
    if (parsed.errors.length) {
      const visibleErrors = parsed.errors.slice(0, 3).join(' ');
      const remainder = parsed.errors.length - 3;
      setFormError(`${visibleErrors}${remainder > 0 ? ` Plus ${remainder} more issue${remainder === 1 ? '' : 's'}.` : ''}`);
      return;
    }
    if (!parsed.contacts.length) {
      setFormError('Add at least one contact in E.164 format.');
      return;
    }

    try {
      const scheduledStart = form.scheduled_start
        ? localDateTimeToIso(form.scheduled_start, timezone)
        : null;
      const scheduledEnd = form.scheduled_end
        ? localDateTimeToIso(form.scheduled_end, timezone)
        : null;
      if (scheduledStart && scheduledEnd && Date.parse(scheduledEnd) <= Date.parse(scheduledStart)) {
        setFormError('Scheduled end must be after scheduled start.');
        return;
      }

      setCreating(true);
      const created = await api.createCampaign({
        name,
        agent_id: form.agent_id,
        timezone,
        calling_hours_start: form.calling_hours_start,
        calling_hours_end: form.calling_hours_end,
        scheduled_start: scheduledStart,
        scheduled_end: scheduledEnd,
        max_concurrent_calls: form.max_concurrent_calls,
        retry_attempts: form.retry_attempts,
        contacts: parsed.contacts,
      });
      setCampaigns((current) => [created, ...current]);
      setForm(initialForm(browserTimezone));
      setShowCreate(false);
      setNotice({
        type: 'success',
        text: `Campaign “${created.name}” was saved as a draft with ${created.total_contacts} contact${created.total_contacts === 1 ? '' : 's'}. No calls have started.`,
      });
    } catch (error: unknown) {
      setFormError(messageFrom(error, 'Could not create the campaign.'));
    } finally {
      setCreating(false);
    }
  };

  const runAction = async (campaign: Campaign, action: 'start' | 'pause') => {
    const workingKey = `${action}-${campaign.id}`;
    setWorking(workingKey);
    setNotice(null);
    try {
      const updated = action === 'pause'
        ? await api.pauseCampaign(campaign.id)
        : await api.startCampaign(campaign.id);
      setCampaigns((current) => current.map((item) => (
        item.id === updated.id ? updated : item
      )));
      setPendingStart(null);
      const actionText = action === 'pause'
        ? 'paused; no new calls will be queued'
        : campaign.status === 'paused' ? 'resumed' : 'started';
      setNotice({ type: 'success', text: `Campaign “${campaign.name}” ${actionText}.` });
    } catch (error: unknown) {
      setNotice({
        type: 'error',
        text: messageFrom(error, `Could not ${action} the campaign.`),
      });
    } finally {
      setWorking('');
    }
  };

  const loadUnknownAttempts = async (campaign: Campaign) => {
    setReviewCampaign(campaign);
    setSelectedAttempt(null);
    setReconciliation(INITIAL_RECONCILIATION);
    setAttemptError('');
    setAttemptsLoadFailed(false);
    setAttemptsLoading(true);
    try {
      setAttempts(await api.listCampaignAttempts(campaign.id, 'unknown'));
    } catch (error: unknown) {
      setAttempts([]);
      setAttemptsLoadFailed(true);
      setAttemptError(messageFrom(error, 'Could not load ambiguous dispatches.'));
    } finally {
      setAttemptsLoading(false);
    }
  };

  const closeAttemptReview = () => {
    setReviewCampaign(null);
    setAttempts([]);
    setAttemptsLoadFailed(false);
    setSelectedAttempt(null);
    setAttemptError('');
    setReconciliation(INITIAL_RECONCILIATION);
  };

  const selectAttemptForReconciliation = (attempt: CampaignAttempt) => {
    setSelectedAttempt(attempt);
    setAttemptError('');
    setReconciliation({
      ...INITIAL_RECONCILIATION,
    });
  };

  const requestCampaignStart = async (campaign: Campaign) => {
    if (campaign.status !== 'paused' || !canReconcileAttempts) {
      setPendingStart(campaign);
      return;
    }

    setReviewCampaign(campaign);
    setSelectedAttempt(null);
    setReconciliation(INITIAL_RECONCILIATION);
    setAttemptError('');
    setAttemptsLoadFailed(false);
    setAttemptsLoading(true);
    try {
      const unknownAttempts = await api.listCampaignAttempts(campaign.id, 'unknown');
      setAttempts(unknownAttempts);
      if (unknownAttempts.length) {
        setNotice({
          type: 'error',
          text: `Review ${unknownAttempts.length} ambiguous provider dispatch${unknownAttempts.length === 1 ? '' : 'es'} before resuming “${campaign.name}”.`,
        });
        return;
      }
      setReviewCampaign(null);
      setPendingStart(campaign);
    } catch (error: unknown) {
      setAttempts([]);
      setAttemptsLoadFailed(true);
      setAttemptError(messageFrom(error, 'Could not verify whether ambiguous dispatches remain.'));
    } finally {
      setAttemptsLoading(false);
    }
  };

  const handleReconciliation = async (event: FormEvent) => {
    event.preventDefault();
    if (!reviewCampaign || !selectedAttempt) return;

    const reason = reconciliation.reason.trim();
    if (reason.length < 3) {
      setAttemptError('Record at least three characters of operator evidence.');
      return;
    }

    const payload: CampaignAttemptReconciliation = { action: 'release_for_retry', reason };

    setReconciling(true);
    setAttemptError('');
    try {
      await api.reconcileCampaignAttempt(reviewCampaign.id, selectedAttempt.id, payload);
    } catch (error: unknown) {
      setAttemptError(messageFrom(error, 'Could not reconcile this dispatch.'));
      setReconciling(false);
      return;
    }

    setAttempts((current) => current.filter((attempt) => attempt.id !== selectedAttempt.id));
    setSelectedAttempt(null);
    setReconciliation(INITIAL_RECONCILIATION);
    setNotice({
      type: 'success',
      text: 'The provider-confirmed unaccepted attempt was released according to the retry policy.',
    });

    const [attemptResult, campaignResult] = await Promise.allSettled([
        api.listCampaignAttempts(reviewCampaign.id, 'unknown'),
        api.listCampaigns(),
    ]);
    const refreshErrors: string[] = [];
    if (attemptResult.status === 'fulfilled') {
      setAttempts(attemptResult.value);
      setAttemptsLoadFailed(false);
    } else {
      setAttemptsLoadFailed(true);
      refreshErrors.push(messageFrom(attemptResult.reason, 'ambiguous dispatches'));
    }
    if (campaignResult.status === 'fulfilled') {
      setCampaigns(campaignResult.value);
    } else {
      refreshErrors.push(messageFrom(campaignResult.reason, 'campaign totals'));
    }
    if (refreshErrors.length) {
      setAttemptError(
        `The reconciliation was saved, but refreshed data could not be loaded (${refreshErrors.join('; ')}). Close and reopen this review before taking another action.`,
      );
    }
    setReconciling(false);
  };

  return (
    <Layout>
      <div className="page-header">
        <div>
          <span className="page-kicker">Outbound operations</span>
          <h1>Campaigns</h1>
          <p className="page-subtitle">Prepare contact lists, schedule compliant calling windows, and control every launch.</p>
        </div>
        <div className="header-actions">
          {canMutateCampaigns ? (
            <button
              className={showCreate ? 'btn btn-secondary' : 'btn btn-primary'}
              type="button"
              onClick={showCreate ? closeCreate : openCreate}
              aria-expanded={showCreate}
              aria-controls="campaign-create-panel"
            >
              {showCreate ? <X size={14} /> : <Plus size={14} />}
              {showCreate ? 'Close' : 'New campaign'}
            </button>
          ) : null}
        </div>
      </div>

      <div className="campaign-messages" aria-live="polite">
        {loadError ? (
          <div className="provider-alert provider-alert-error campaign-message" role="alert">
            <CircleAlert size={15} />
            <span>{loadError}</span>
            <button className="btn btn-secondary btn-sm" type="button" onClick={retryLoad} disabled={loading}>
              <RotateCw size={12} /> Retry
            </button>
          </div>
        ) : null}
        {notice ? (
          <div className={`provider-alert campaign-message ${notice.type === 'error' ? 'provider-alert-error' : 'campaign-message-success'}`} role={notice.type === 'error' ? 'alert' : 'status'}>
            {notice.type === 'error' ? <CircleAlert size={15} /> : <CheckCircle2 size={15} />}
            <span>{notice.text}</span>
          </div>
        ) : null}
      </div>

      {pendingStart ? (
        <section id="campaign-start-confirmation" className="campaign-confirmation" aria-labelledby="campaign-confirmation-title">
          <div className="campaign-confirmation-icon"><Play size={18} /></div>
          <div>
            <span className="page-kicker">Operator confirmation</span>
            <h2 id="campaign-confirmation-title">
              {pendingStart.status === 'paused' ? 'Resume' : 'Start'} “{pendingStart.name}”?
            </h2>
            <p>
              This can place real outbound calls to {pendingStart.total_contacts} contact{pendingStart.total_contacts === 1 ? '' : 's'}.
              Calls will follow {pendingStart.calling_hours_start}–{pendingStart.calling_hours_end} in {pendingStart.timezone}.
            </p>
          </div>
          <div className="campaign-confirmation-actions">
            <button className="btn btn-secondary" type="button" onClick={() => setPendingStart(null)} disabled={Boolean(working)}>Cancel</button>
            <button className="btn btn-primary" type="button" onClick={() => void runAction(pendingStart, 'start')} disabled={Boolean(working)}>
              <Play size={13} /> {working ? 'Working…' : pendingStart.status === 'paused' ? 'Resume calls' : 'Start calls'}
            </button>
          </div>
        </section>
      ) : null}

      {reviewCampaign && canReconcileAttempts ? (
        <section className="card campaign-create-panel" aria-labelledby="campaign-attempt-review-title">
          <div className="campaign-create-heading">
            <div className="campaign-create-icon"><CircleAlert size={19} /></div>
            <div>
              <span className="page-kicker">Duplicate-call safety</span>
              <h2 id="campaign-attempt-review-title">Review “{reviewCampaign.name}” dispatches</h2>
              <p>
                The campaign remains paused until every ambiguous provider dispatch is resolved.
                Verify each attempt in the provider console before taking action.
              </p>
            </div>
            <button className="btn btn-secondary btn-sm" type="button" onClick={closeAttemptReview} disabled={reconciling}>
              <X size={12} /> Close
            </button>
          </div>

          {attemptError ? <div className="inline-error campaign-form-error" role="alert">{attemptError}</div> : null}
          {attemptsLoading ? (
            <div className="page-loading" role="status">Loading ambiguous dispatches…</div>
          ) : !attemptsLoadFailed && attempts.length === 0 ? (
            <div className="provider-alert campaign-message-success" role="status">
              <CheckCircle2 size={15} />
              <span>No unknown provider dispatches remain for this campaign.</span>
            </div>
          ) : (
            <div className="table-container">
              <table>
                <thead>
                  <tr>
                    <th>Attempt</th>
                    <th>Provider</th>
                    <th>Dispatch started</th>
                    <th>Worker evidence</th>
                    <th><span className="visually-hidden">Action</span></th>
                  </tr>
                </thead>
                <tbody>
                  {attempts.map((attempt) => (
                    <tr key={attempt.id}>
                      <td>
                        <strong>Attempt {attempt.attempt_number}</strong>
                        <span className="campaign-secondary">Attempt ID: <code>{attempt.id}</code></span>
                        <span className="campaign-secondary">Local call ID: <code>{attempt.call_id || 'not recorded'}</code></span>
                      </td>
                      <td>
                        <span className="badge badge-warning">{attempt.state}</span>
                        <span className="campaign-secondary">{attempt.provider}</span>
                        <span className="campaign-secondary">Provider call ID: <code>{attempt.provider_call_sid || 'not recorded'}</code></span>
                      </td>
                      <td>{attempt.dispatch_started_at ? new Date(attempt.dispatch_started_at).toLocaleString() : 'Unknown'}</td>
                      <td>{attempt.error_message || 'Provider acceptance could not be proven.'}</td>
                      <td className="campaign-action-cell">
                        <button
                          className="btn btn-secondary btn-sm"
                          type="button"
                          onClick={() => selectAttemptForReconciliation(attempt)}
                          disabled={reconciling}
                        >
                          Review evidence
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {selectedAttempt ? (
            <form onSubmit={handleReconciliation} noValidate>
              <fieldset className="campaign-form-fields" disabled={reconciling}>
                <legend>Resolve attempt {selectedAttempt.attempt_number}</legend>
                <div className="provider-alert provider-alert-error" role="alert">
                  <CircleAlert size={15} />
                  <span>
                    Workspace users cannot attach provider call identities. If a call exists, keep the campaign paused and await a signed callback or trusted platform reconciliation. Release only when the provider proves no call was created.
                  </span>
                </div>
                <div className="campaign-form-grid">
                  <div className="form-group campaign-span-full">
                    <label htmlFor="campaign-reconciliation-reason">Operator evidence</label>
                    <textarea
                      id="campaign-reconciliation-reason"
                      value={reconciliation.reason}
                      onChange={(event) => setReconciliation((current) => ({
                        ...current,
                        reason: event.target.value,
                      }))}
                      maxLength={500}
                      placeholder="What was verified, where, and by whom?"
                      required
                    />
                    <p className="form-hint">This reason is written to the workspace audit log.</p>
                  </div>
                </div>
                <div className="campaign-form-actions">
                  <button className="btn btn-secondary" type="button" onClick={() => setSelectedAttempt(null)}>Cancel</button>
                  <button className="btn btn-danger" type="submit">
                    {reconciling ? 'Recording…' : 'Release verified attempt'}
                  </button>
                </div>
              </fieldset>
            </form>
          ) : null}
        </section>
      ) : null}

      {showCreate ? (
        <section id="campaign-create-panel" className="card campaign-create-panel" aria-labelledby="campaign-create-title">
          <div className="campaign-create-heading">
            <div className="campaign-create-icon"><CalendarClock size={19} /></div>
            <div>
              <h2 id="campaign-create-title">Configure a safe campaign draft</h2>
              <p>Nothing calls automatically. Review the saved draft, then explicitly start it from the campaign table.</p>
            </div>
          </div>

          {provisionedAgents.length === 0 ? (
            <div className="provider-alert provider-alert-error" role="alert">
              <CircleAlert size={15} />
              <span>You need an active, provisioned Smallest.ai agent before creating a campaign.</span>
              <Link href="/agents" className="btn btn-secondary btn-sm">Open agents</Link>
            </div>
          ) : null}

          <form onSubmit={handleCreate} noValidate>
            {formError ? <div className="inline-error campaign-form-error" role="alert">{formError}</div> : null}
            <fieldset className="campaign-form-fields" disabled={creating}>
              <legend className="visually-hidden">Campaign settings</legend>
              <div className="campaign-form-grid">
                <div className="form-group">
                  <label htmlFor="campaign-name">Campaign name</label>
                  <input
                    id="campaign-name"
                    value={form.name}
                    onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
                    maxLength={255}
                    autoComplete="off"
                    required
                  />
                </div>
                <div className="form-group">
                  <label htmlFor="campaign-agent">Provisioned agent</label>
                  <select
                    id="campaign-agent"
                    value={form.agent_id}
                    onChange={(event) => setForm((current) => ({ ...current, agent_id: event.target.value }))}
                    required
                  >
                    <option value="">Select an agent</option>
                    {provisionedAgents.map((agent) => (
                      <option key={agent.id} value={agent.id}>{agent.name} · {agent.sync_status.replace('_', ' ')}</option>
                    ))}
                  </select>
                </div>
                <div className="form-group campaign-timezone-field">
                  <label htmlFor="campaign-timezone">IANA timezone</label>
                  <input
                    id="campaign-timezone"
                    list="campaign-timezones"
                    value={form.timezone}
                    onChange={(event) => setForm((current) => ({ ...current, timezone: event.target.value }))}
                    autoComplete="off"
                    required
                  />
                  <datalist id="campaign-timezones">
                    {timezoneOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                  </datalist>
                  <p className="form-hint">Defaults to your browser timezone ({browserTimezone}). UAE: Asia/Dubai · India: Asia/Kolkata.</p>
                </div>
                <div className="campaign-window-grid">
                  <div className="form-group">
                    <label htmlFor="campaign-window-start">Calling window starts</label>
                    <input
                      id="campaign-window-start"
                      type="time"
                      value={form.calling_hours_start}
                      onChange={(event) => setForm((current) => ({ ...current, calling_hours_start: event.target.value }))}
                      required
                    />
                  </div>
                  <div className="form-group">
                    <label htmlFor="campaign-window-end">Calling window ends</label>
                    <input
                      id="campaign-window-end"
                      type="time"
                      value={form.calling_hours_end}
                      onChange={(event) => setForm((current) => ({ ...current, calling_hours_end: event.target.value }))}
                      required
                    />
                  </div>
                </div>
                <div className="form-group">
                  <label htmlFor="campaign-scheduled-start">Scheduled start <span>Optional</span></label>
                  <input
                    id="campaign-scheduled-start"
                    type="datetime-local"
                    value={form.scheduled_start}
                    onChange={(event) => setForm((current) => ({ ...current, scheduled_start: event.target.value }))}
                  />
                  <p className="form-hint">Interpreted in the campaign timezone.</p>
                </div>
                <div className="form-group">
                  <label htmlFor="campaign-scheduled-end">Scheduled end <span>Optional</span></label>
                  <input
                    id="campaign-scheduled-end"
                    type="datetime-local"
                    value={form.scheduled_end}
                    onChange={(event) => setForm((current) => ({ ...current, scheduled_end: event.target.value }))}
                  />
                  <p className="form-hint">No new calls begin after this time.</p>
                </div>
                <div className="form-group">
                  <label htmlFor="campaign-concurrency">Concurrent calls</label>
                  <input
                    id="campaign-concurrency"
                    type="number"
                    min={1}
                    max={100}
                    value={form.max_concurrent_calls}
                    onChange={(event) => setForm((current) => ({ ...current, max_concurrent_calls: Number(event.target.value) }))}
                    required
                  />
                  <p className="form-hint">Between 1 and 100, subject to your account limit.</p>
                </div>
                <div className="form-group">
                  <label htmlFor="campaign-retries">Retry attempts</label>
                  <input
                    id="campaign-retries"
                    type="number"
                    min={0}
                    max={10}
                    value={form.retry_attempts}
                    onChange={(event) => setForm((current) => ({ ...current, retry_attempts: Number(event.target.value) }))}
                    required
                  />
                  <p className="form-hint">Between 0 and 10 per contact.</p>
                </div>
                <div className="form-group campaign-span-full">
                  <label htmlFor="campaign-contacts">Contacts <span>{contactLineCount.toLocaleString()} line{contactLineCount === 1 ? '' : 's'}</span></label>
                  <textarea
                    id="campaign-contacts"
                    className="campaign-contacts-input"
                    value={form.contacts}
                    onChange={(event) => setForm((current) => ({ ...current, contacts: event.target.value }))}
                    placeholder={'+971501234567, Aisha\n+919876543210, Rohan\n+14155550123'}
                    aria-describedby="campaign-contacts-help"
                    spellCheck={false}
                    required
                  />
                  <p id="campaign-contacts-help" className="form-hint">One E.164 number per line, optionally followed by a comma and name. Duplicates are rejected before saving.</p>
                </div>
              </div>
              <div className="campaign-form-actions">
                <button className="btn btn-secondary" type="button" onClick={closeCreate}>Cancel</button>
                <button className="btn btn-primary" type="submit" disabled={provisionedAgents.length === 0 || creating}>
                  <CheckCircle2 size={13} /> {creating ? 'Saving draft…' : 'Save campaign draft'}
                </button>
              </div>
            </fieldset>
          </form>
        </section>
      ) : null}

      {loading ? (
        <div className="page-loading" role="status">Loading campaign operations…</div>
      ) : campaigns.length === 0 && !loadError ? (
        <div className="empty-state">
          <div className="empty-state-icon"><Users size={23} /></div>
          <h3>No campaigns yet</h3>
          <p>Build a reviewed draft with a provisioned agent, compliant calling hours, and a validated contact list.</p>
          {canMutateCampaigns ? (
            <button className="btn btn-primary" type="button" onClick={openCreate}><Plus size={14} /> Create first campaign</button>
          ) : null}
        </div>
      ) : campaigns.length ? (
        <div className="table-container campaign-table" aria-busy={Boolean(working)}>
          <table>
            <thead>
              <tr>
                <th>Campaign</th>
                <th>Agent</th>
                <th>Status</th>
                <th>Calling policy</th>
                <th>Progress</th>
                <th>Outcome</th>
                <th><span className="visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {campaigns.map((campaign) => {
                const completed = Math.min(campaign.completed_contacts, campaign.total_contacts);
                const successRate = campaign.total_contacts
                  ? Math.round((campaign.successful_contacts / campaign.total_contacts) * 100)
                  : 0;
                const starts = formatSchedule(campaign.scheduled_start, campaign.timezone);
                const ends = formatSchedule(campaign.scheduled_end, campaign.timezone);
                return (
                  <tr key={campaign.id}>
                    <td>
                      <strong className="campaign-name">{campaign.name}</strong>
                      <span className="campaign-secondary">
                        {starts ? `Starts ${starts}` : 'Manual start'}{ends ? ` · Ends ${ends}` : ''}
                      </span>
                    </td>
                    <td>
                      <span className="campaign-agent-name">{campaign.agent_id ? agentNames.get(campaign.agent_id) || 'Provisioned agent' : 'No agent'}</span>
                    </td>
                    <td><span className={`badge ${statusBadge(campaign.status)}`}>{campaign.status}</span></td>
                    <td>
                      <span className="campaign-policy"><Clock3 size={11} /> {campaign.calling_hours_start || '—'}–{campaign.calling_hours_end || '—'}</span>
                      <span className="campaign-secondary">{campaign.timezone} · {campaign.max_concurrent_calls} concurrent · {campaign.retry_attempts} retries</span>
                    </td>
                    <td>
                      <span className="campaign-progress-label">{completed.toLocaleString()} / {campaign.total_contacts.toLocaleString()}</span>
                      <progress
                        className="campaign-progress"
                        max={Math.max(campaign.total_contacts, 1)}
                        value={completed}
                        aria-label={`${campaign.name}: ${completed} of ${campaign.total_contacts} contacts completed`}
                      />
                    </td>
                    <td>
                      <strong className="campaign-outcome">{campaign.successful_contacts.toLocaleString()} successful</strong>
                      <span className="campaign-secondary">{successRate}% of contacts</span>
                    </td>
                    <td className="campaign-action-cell">
                      {canReconcileAttempts && campaign.status === 'paused' ? (
                        <button
                          className="btn btn-secondary btn-sm"
                          type="button"
                          onClick={() => void loadUnknownAttempts(campaign)}
                          disabled={attemptsLoading || reconciling}
                          aria-controls="campaign-attempt-review-title"
                        >
                          <CircleAlert size={12} /> Review dispatches
                        </button>
                      ) : null}
                      {canMutateCampaigns && (campaign.status === 'draft' || campaign.status === 'paused') ? (
                        <button
                          className="btn btn-primary btn-sm"
                          type="button"
                          onClick={() => void requestCampaignStart(campaign)}
                          disabled={
                            Boolean(working)
                            || attemptsLoading
                            || campaign.total_contacts === 0
                            || (
                              reviewCampaign?.id === campaign.id
                              && (attemptsLoadFailed || attempts.length > 0)
                            )
                          }
                          aria-controls="campaign-start-confirmation"
                        >
                          <Play size={12} /> {campaign.status === 'paused' ? 'Resume' : 'Start'}
                        </button>
                      ) : null}
                      {canMutateCampaigns && campaign.status === 'running' ? (
                        <button
                          className="btn btn-secondary btn-sm"
                          type="button"
                          onClick={() => void runAction(campaign, 'pause')}
                          disabled={Boolean(working)}
                        >
                          <Pause size={12} /> {working === `pause-${campaign.id}` ? 'Pausing…' : 'Pause'}
                        </button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </Layout>
  );
}
