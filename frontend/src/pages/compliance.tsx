import Head from 'next/head';
import { useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  FileCheck2,
  History,
  RotateCcw,
  Search,
  ShieldCheck,
} from 'lucide-react';
import Layout from '@/components/Layout';
import { api, type ConsentRecord, type ConsentType } from '@/lib/api';
import shellAccessibility from '@/lib/shell-accessibility.cjs';
import styles from '@/styles/Compliance.module.css';

const { E164_PATTERN } = shellAccessibility;

type Feedback = { type: 'error' | 'success'; message: string } | null;
type ConsentStatus = ConsentRecord['status'];
type EvidenceMethod = 'verbal_call' | 'web_form' | 'signed_document' | 'email' | 'sms' | 'import';

const CONSENT_TYPE_LABELS: Record<ConsentType, string> = {
  outbound_call: 'Outbound call',
  marketing_call: 'Marketing call',
  recording: 'Call recording',
  data_processing: 'Data processing',
};

const EVIDENCE_METHOD_LABELS: Record<EvidenceMethod, string> = {
  verbal_call: 'Verbal confirmation',
  web_form: 'Web form',
  signed_document: 'Signed document',
  email: 'Email',
  sms: 'SMS',
  import: 'Verified import',
};

function consentTimestamp(record: ConsentRecord) {
  return record.status === 'granted'
    ? (record.granted_at ?? record.created_at)
    : (record.revoked_at ?? record.created_at);
}

function evidenceText(record: ConsentRecord, key: 'method' | 'reference') {
  const value = record.evidence?.[key];
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function formatTimestamp(value: string) {
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return 'Timestamp unavailable';
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(timestamp);
}

export default function Compliance() {
  const [checkNumber, setCheckNumber] = useState('');
  const [checkResult, setCheckResult] = useState<{ is_on_dnc: boolean } | null>(null);
  const [checkFeedback, setCheckFeedback] = useState<Feedback>(null);
  const [checking, setChecking] = useState(false);
  const [addNumber, setAddNumber] = useState('');
  const [addReason, setAddReason] = useState('');
  const [addFeedback, setAddFeedback] = useState<Feedback>(null);
  const [adding, setAdding] = useState(false);

  const [consentRecords, setConsentRecords] = useState<ConsentRecord[]>([]);
  const [recordsLoading, setRecordsLoading] = useState(true);
  const [recordsError, setRecordsError] = useState('');
  const [reloadToken, setReloadToken] = useState(0);
  const [phoneFilter, setPhoneFilter] = useState('');
  const [typeFilter, setTypeFilter] = useState<ConsentType | ''>('');
  const [statusFilter, setStatusFilter] = useState<ConsentStatus | ''>('');

  const [consentPhone, setConsentPhone] = useState('');
  const [consentType, setConsentType] = useState<ConsentType>('outbound_call');
  const [consentStatus, setConsentStatus] = useState<ConsentStatus | ''>('');
  const [evidenceMethod, setEvidenceMethod] = useState<EvidenceMethod | ''>('');
  const [evidenceReference, setEvidenceReference] = useState('');
  const [consentFeedback, setConsentFeedback] = useState<Feedback>(null);
  const [savingConsent, setSavingConsent] = useState(false);

  useEffect(() => {
    let cancelled = false;

    const loadConsentRecords = async () => {
      setRecordsLoading(true);
      setRecordsError('');
      try {
        const records = await api.listConsentRecords();
        if (!cancelled) setConsentRecords(records);
      } catch (error: unknown) {
        if (!cancelled) {
          setRecordsError(error instanceof Error ? error.message : 'Could not load consent history.');
        }
      } finally {
        if (!cancelled) setRecordsLoading(false);
      }
    };

    void loadConsentRecords();
    return () => { cancelled = true; };
  }, [reloadToken]);

  const filteredConsentRecords = useMemo(() => {
    const phoneQuery = phoneFilter.trim().replace(/[^\d+]/g, '');
    return consentRecords.filter((record) => {
      if (phoneQuery && !record.phone_number.includes(phoneQuery)) return false;
      if (typeFilter && record.consent_type !== typeFilter) return false;
      if (statusFilter && record.status !== statusFilter) return false;
      return true;
    });
  }, [consentRecords, phoneFilter, statusFilter, typeFilter]);

  const filtersActive = Boolean(phoneFilter || typeFilter || statusFilter);

  const handleCheck = async (e: React.FormEvent) => {
    e.preventDefault();
    setCheckFeedback(null);
    setCheckResult(null);
    setChecking(true);
    try {
      const result = await api.checkDnc(checkNumber);
      setCheckResult(result);
    } catch (error: unknown) {
      setCheckFeedback({
        type: 'error',
        message: error instanceof Error ? error.message : 'Could not check the DNC list.',
      });
    } finally {
      setChecking(false);
    }
  };

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    setAddFeedback(null);
    setAdding(true);
    try {
      await api.addDnc({ phone_number: addNumber, reason: addReason || undefined });
      setAddNumber('');
      setAddReason('');
      setAddFeedback({ type: 'success', message: 'Number added to the DNC list.' });
    } catch (error: unknown) {
      setAddFeedback({
        type: 'error',
        message: error instanceof Error ? error.message : 'Could not update the DNC list.',
      });
    } finally {
      setAdding(false);
    }
  };

  const handleConsentRecord = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setConsentFeedback(null);
    if (!consentStatus || !evidenceMethod) {
      setConsentFeedback({ type: 'error', message: 'Choose a decision and evidence method before recording this event.' });
      return;
    }
    setSavingConsent(true);
    try {
      const record = await api.createConsentRecord({
        phone_number: consentPhone,
        consent_type: consentType,
        status: consentStatus,
        evidence: {
          method: evidenceMethod,
          reference: evidenceReference.trim(),
        },
      });
      setConsentRecords((current) => [record, ...current.filter((item) => item.id !== record.id)]);
      setConsentPhone(record.phone_number);
      setConsentStatus('');
      setEvidenceMethod('');
      setEvidenceReference('');
      setConsentFeedback({
        type: 'success',
        message: `${CONSENT_TYPE_LABELS[record.consent_type]} ${record.status === 'granted' ? 'grant' : 'revocation'} recorded. The previous history remains unchanged.`,
      });
    } catch (error: unknown) {
      setConsentFeedback({
        type: 'error',
        message: error instanceof Error ? error.message : 'Could not record the consent event.',
      });
    } finally {
      setSavingConsent(false);
    }
  };

  const clearConsentFilters = () => {
    setPhoneFilter('');
    setTypeFilter('');
    setStatusFilter('');
  };

  return (
    <Layout>
      <Head>
        <title>Compliance controls | VAV Voice AI</title>
        <meta name="description" content="Manage do-not-call controls and review voice compliance safeguards." />
      </Head>
      <div className="page-header">
        <div>
          <span className="page-kicker">Policy &amp; protection</span>
          <h1>Compliance controls</h1>
          <p className="page-subtitle">Maintain suppression controls and an append-only record of customer consent decisions.</p>
        </div>
      </div>

      <section className={`card ${styles.consentSection}`} aria-labelledby="consent-management-title">
        <div className={styles.consentHeading}>
          <div className="compliance-card-heading">
            <span className="compliance-card-icon"><History size={20} aria-hidden="true" /></span>
            <div>
              <h2 id="consent-management-title">Consent management</h2>
              <p>Record new events without changing or deleting the audit history.</p>
            </div>
          </div>
          <span className="badge badge-info"><History size={12} aria-hidden="true" /> Append-only log</span>
        </div>

        <div className={styles.policyNotice} role="note" aria-labelledby="consent-policy-title">
          <AlertTriangle size={20} aria-hidden="true" />
          <div>
            <h3 id="consent-policy-title">Consent records need policy context</h3>
            <p><strong>No record is not a grant.</strong> Confirm your lawful basis and required disclosures separately. Only the latest explicit <strong>outbound-call</strong> or <strong>marketing-call</strong> revocation blocks direct and campaign dispatch. Recording and data-processing events are retained as audit evidence but are not dialer enforcement controls.</p>
          </div>
        </div>

        <div className={styles.consentWorkspace}>
          <form className={styles.consentForm} onSubmit={handleConsentRecord} aria-busy={savingConsent} aria-describedby="consent-form-help">
            <div className={styles.formHeading}>
              <h3>Record consent event</h3>
              <p id="consent-form-help">A later event for the same number and scope becomes the current decision; earlier events stay in history.</p>
            </div>

            <div className="form-group">
              <label htmlFor="consent-phone-number">Phone number</label>
              <input
                id="consent-phone-number"
                type="tel"
                inputMode="tel"
                autoComplete="tel"
                value={consentPhone}
                onChange={(event) => {
                  setConsentPhone(event.target.value);
                  setConsentFeedback(null);
                }}
                placeholder="+971501234567"
                pattern={E164_PATTERN}
                aria-describedby="consent-phone-hint"
                required
              />
              <p id="consent-phone-hint" className="form-hint">Use international E.164 format, including the leading +.</p>
            </div>

            <div className="form-group">
              <label htmlFor="consent-scope">Consent scope</label>
              <select
                id="consent-scope"
                value={consentType}
                onChange={(event) => {
                  setConsentType(event.target.value as ConsentType);
                  setConsentFeedback(null);
                }}
                required
              >
                {Object.entries(CONSENT_TYPE_LABELS).map(([value, label]) => (
                  <option value={value} key={value}>{label}</option>
                ))}
              </select>
            </div>

            <fieldset className={styles.statusFieldset}>
              <legend>Decision</legend>
              <div className={styles.statusOptions}>
                <label className={consentStatus === 'granted' ? styles.statusOptionSelected : undefined}>
                  <input
                    type="radio"
                    name="consent-status"
                    value="granted"
                    checked={consentStatus === 'granted'}
                    required
                    onChange={() => {
                      setConsentStatus('granted');
                      setConsentFeedback(null);
                    }}
                  />
                  <CheckCircle2 size={15} aria-hidden="true" />
                  Grant
                </label>
                <label className={consentStatus === 'revoked' ? styles.statusOptionSelectedDanger : undefined}>
                  <input
                    type="radio"
                    name="consent-status"
                    value="revoked"
                    checked={consentStatus === 'revoked'}
                    onChange={() => {
                      setConsentStatus('revoked');
                      setConsentFeedback(null);
                    }}
                  />
                  <ShieldCheck size={15} aria-hidden="true" />
                  Revoke
                </label>
              </div>
            </fieldset>

            <div className="form-group">
              <label htmlFor="evidence-method">Evidence method</label>
              <select
                id="evidence-method"
                value={evidenceMethod}
                onChange={(event) => {
                  setEvidenceMethod(event.target.value as EvidenceMethod | '');
                  setConsentFeedback(null);
                }}
                required
              >
                <option value="">Select method</option>
                {Object.entries(EVIDENCE_METHOD_LABELS).map(([value, label]) => (
                  <option value={value} key={value}>{label}</option>
                ))}
              </select>
            </div>

            <div className="form-group">
              <label htmlFor="evidence-reference">Evidence reference</label>
              <input
                id="evidence-reference"
                type="text"
                value={evidenceReference}
                onChange={(event) => {
                  setEvidenceReference(event.target.value);
                  setConsentFeedback(null);
                }}
                placeholder="Call ID, form submission, or document ID"
                maxLength={240}
                aria-describedby="evidence-reference-hint"
                required
              />
              <p id="evidence-reference-hint" className="form-hint">Store a reference identifier, not sensitive evidence content.</p>
            </div>

            <div className={consentStatus === 'revoked' ? styles.decisionImpactDanger : styles.decisionImpact} role="note">
              {!consentStatus
                ? 'Choose a grant or revocation to review how this event will be used.'
                : consentStatus === 'revoked' && (consentType === 'outbound_call' || consentType === 'marketing_call')
                ? 'This revocation will block direct and campaign dispatch to this number.'
                : consentStatus === 'granted' && (consentType === 'outbound_call' || consentType === 'marketing_call')
                  ? 'This grant supersedes an earlier revocation for the same scope; all other policy checks still apply.'
                  : 'This event is recorded for audit history and is not enforced by the dialer.'}
            </div>

            <button type="submit" className={consentStatus === 'revoked' ? 'btn btn-danger' : 'btn btn-primary'} disabled={savingConsent || !consentStatus}>
              {savingConsent ? 'Recording…' : consentStatus ? `Record ${consentStatus === 'granted' ? 'grant' : 'revocation'}` : 'Choose a decision'}
            </button>

            {consentFeedback ? (
              <div
                className={`compliance-feedback compliance-feedback-${consentFeedback.type}`}
                role={consentFeedback.type === 'error' ? 'alert' : 'status'}
                aria-live={consentFeedback.type === 'error' ? 'assertive' : 'polite'}
              >
                {consentFeedback.message}
              </div>
            ) : null}
          </form>

          <div className={styles.historyPanel}>
            <div className={styles.historyHeading}>
              <div>
                <h3>Recent consent records</h3>
                <p>Newest first. Events are never edited in place.</p>
              </div>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => setReloadToken((current) => current + 1)}
                disabled={recordsLoading}
              >
                <RotateCcw size={13} aria-hidden="true" /> {recordsLoading ? 'Refreshing…' : 'Refresh'}
              </button>
            </div>

            <fieldset className={styles.historyFilters}>
              <legend className="visually-hidden">Filter consent records</legend>
              <div className="form-group">
                <label htmlFor="consent-phone-filter">Search by phone</label>
                <div className={styles.searchControl}>
                  <Search size={15} aria-hidden="true" />
                  <input
                    id="consent-phone-filter"
                    type="search"
                    inputMode="tel"
                    value={phoneFilter}
                    onChange={(event) => setPhoneFilter(event.target.value)}
                    placeholder="Full number or digits"
                  />
                </div>
              </div>
              <div className="form-group">
                <label htmlFor="consent-type-filter">Scope</label>
                <select id="consent-type-filter" value={typeFilter} onChange={(event) => setTypeFilter(event.target.value as ConsentType | '')}>
                  <option value="">All scopes</option>
                  {Object.entries(CONSENT_TYPE_LABELS).map(([value, label]) => (
                    <option value={value} key={value}>{label}</option>
                  ))}
                </select>
              </div>
              <div className="form-group">
                <label htmlFor="consent-status-filter">Decision</label>
                <select id="consent-status-filter" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as ConsentStatus | '')}>
                  <option value="">All decisions</option>
                  <option value="granted">Granted</option>
                  <option value="revoked">Revoked</option>
                </select>
              </div>
              <button type="button" className="btn btn-secondary" onClick={clearConsentFilters} disabled={!filtersActive}>
                Clear filters
              </button>
            </fieldset>

            <p className={styles.resultSummary} role="status" aria-live="polite">
              {recordsLoading ? 'Loading consent history…' : `Showing ${filteredConsentRecords.length} of ${consentRecords.length} records.`}
            </p>

            {recordsError ? (
              <div className="compliance-feedback compliance-feedback-error" role="alert">
                <span>{recordsError}</span>
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => setReloadToken((current) => current + 1)}>Try again</button>
              </div>
            ) : recordsLoading ? (
              <div className={styles.historyLoading} role="status">Loading consent history…</div>
            ) : filteredConsentRecords.length === 0 ? (
              <div className={styles.historyEmpty}>
                <History size={24} aria-hidden="true" />
                <h4>{consentRecords.length ? 'No records match these filters' : 'No consent events recorded'}</h4>
                <p>{consentRecords.length ? 'Clear or adjust the filters to broaden the results.' : 'New grants and revocations will appear here without replacing earlier history.'}</p>
                {filtersActive ? <button type="button" className="btn btn-secondary btn-sm" onClick={clearConsentFilters}>Clear filters</button> : null}
              </div>
            ) : (
              <div className={`table-container ${styles.historyTable}`} tabIndex={0} role="region" aria-label="Consent record history">
                <table>
                  <caption className="visually-hidden">Append-only consent record history, newest first</caption>
                  <thead>
                    <tr>
                      <th scope="col">Phone</th>
                      <th scope="col">Scope</th>
                      <th scope="col">Decision</th>
                      <th scope="col">Evidence</th>
                      <th scope="col">Recorded</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredConsentRecords.map((record) => {
                      const method = evidenceText(record, 'method');
                      const reference = evidenceText(record, 'reference');
                      const timestamp = consentTimestamp(record);
                      return (
                        <tr key={record.id}>
                          <td><span className="phone-value">{record.phone_number}</span></td>
                          <td>{CONSENT_TYPE_LABELS[record.consent_type] ?? record.consent_type.replace(/_/g, ' ')}</td>
                          <td><span className={`badge ${record.status === 'granted' ? 'badge-success' : 'badge-danger'}`}>{record.status}</span></td>
                          <td>
                            <span className={styles.evidenceMethod}>
                              {method && Object.prototype.hasOwnProperty.call(EVIDENCE_METHOD_LABELS, method)
                                ? EVIDENCE_METHOD_LABELS[method as EvidenceMethod]
                                : (method?.replace(/_/g, ' ') ?? 'Not provided')}
                            </span>
                            {reference ? <span className={styles.evidenceReference}>{reference}</span> : null}
                          </td>
                          <td><time dateTime={timestamp}>{formatTimestamp(timestamp)}</time></td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </section>

      <div className="compliance-grid">
        <section className="card compliance-card" aria-labelledby="check-dnc-title">
          <div className="compliance-card-heading">
            <span className="compliance-card-icon"><ShieldCheck size={20} aria-hidden="true" /></span>
            <div><h2 id="check-dnc-title">Check DNC list</h2><p>Verify a number before an outbound call.</p></div>
          </div>
          <form onSubmit={handleCheck} aria-describedby="check-number-hint" aria-busy={checking}>
            <div className="form-group">
              <label htmlFor="check-phone-number">Phone number</label>
              <input
                id="check-phone-number"
                type="tel"
                inputMode="tel"
                autoComplete="tel"
                value={checkNumber}
                onChange={(e) => {
                  setCheckNumber(e.target.value);
                  setCheckResult(null);
                  setCheckFeedback(null);
                }}
                placeholder="+971501234567"
                pattern={E164_PATTERN}
                aria-describedby="check-number-hint"
                required
              />
              <p id="check-number-hint" className="form-hint">Use international E.164 format, including the leading +.</p>
            </div>
            <button type="submit" className="btn btn-primary" disabled={checking}>
              {checking ? 'Checking…' : 'Check number'}
            </button>
          </form>
          {checkFeedback ? (
            <div className="compliance-feedback compliance-feedback-error" role="alert">
              {checkFeedback.message}
            </div>
          ) : null}
          {checkResult !== null && (
            <div className="compliance-result" role="status" aria-live="polite" aria-atomic="true">
              <span>Check complete</span>
              <span className={`badge ${checkResult.is_on_dnc ? 'badge-danger' : 'badge-success'}`}>
                {checkResult.is_on_dnc ? 'On DNC list' : 'Not on DNC list'}
              </span>
            </div>
          )}
          {checkResult?.is_on_dnc === false ? <p className={styles.dncClarification}>A clear DNC result does not prove consent or another lawful basis.</p> : null}
        </section>

        <section className="card compliance-card" aria-labelledby="add-dnc-title">
          <div className="compliance-card-heading">
            <span className="compliance-card-icon compliance-card-icon-danger"><FileCheck2 size={20} aria-hidden="true" /></span>
            <div><h2 id="add-dnc-title">Add to DNC list</h2><p>Prevent future outbound calls to a number.</p></div>
          </div>
          <form onSubmit={handleAdd} aria-describedby="add-number-hint" aria-busy={adding}>
            <div className="form-group">
              <label htmlFor="add-phone-number">Phone number</label>
              <input
                id="add-phone-number"
                type="tel"
                inputMode="tel"
                autoComplete="tel"
                value={addNumber}
                onChange={(e) => {
                  setAddNumber(e.target.value);
                  setAddFeedback(null);
                }}
                placeholder="+971501234567"
                pattern={E164_PATTERN}
                aria-describedby="add-number-hint"
                required
              />
              <p id="add-number-hint" className="form-hint">Use international E.164 format, including the leading +.</p>
            </div>
            <div className="form-group">
              <label htmlFor="dnc-reason">Reason <span>Optional</span></label>
              <select
                id="dnc-reason"
                value={addReason}
                onChange={(e) => {
                  setAddReason(e.target.value);
                  setAddFeedback(null);
                }}
              >
                <option value="">Select reason</option>
                <option value="customer_request">Customer request</option>
                <option value="regulatory">Regulatory</option>
                <option value="complaint">Complaint</option>
              </select>
            </div>
            <button type="submit" className="btn btn-danger" disabled={adding}>
              {adding ? 'Adding…' : 'Add to DNC'}
            </button>
          </form>
          {addFeedback ? (
            <div
              className={`compliance-feedback compliance-feedback-${addFeedback.type}`}
              role={addFeedback.type === 'error' ? 'alert' : 'status'}
              aria-live={addFeedback.type === 'error' ? 'assertive' : 'polite'}
            >
              {addFeedback.message}
            </div>
          ) : null}
        </section>
      </div>

      <section className="card compliance-capabilities" aria-labelledby="compliance-capabilities-title">
        <div className="card-title">
          <div><h2 id="compliance-capabilities-title">Safeguard coverage</h2><p>Current protection available in this workspace.</p></div>
        </div>
        <div className="compliance-features-grid">
          <article className="compliance-feature-card">
            <ShieldCheck size={20} aria-hidden="true" />
            <div><h3>DNC management</h3><p>Check and maintain a tenant-isolated suppression list.</p></div>
            <span className="badge badge-success">Operational</span>
          </article>
          <article className="compliance-feature-card">
            <FileCheck2 size={20} aria-hidden="true" />
            <div><h3>Consent revocations</h3><p>Explicit outbound and marketing-call revocations recorded through the API are enforced immediately before direct and campaign dispatch.</p></div>
            <span className="badge badge-success">Enforced</span>
          </article>
          <article className="compliance-feature-card">
            <Clock3 size={20} aria-hidden="true" />
            <div><h3>Calling hours</h3><p>Calling windows are enforced for campaigns and configured per campaign timezone.</p></div>
            <span className="badge badge-info">Campaign scoped</span>
          </article>
        </div>
      </section>
    </Layout>
  );
}
