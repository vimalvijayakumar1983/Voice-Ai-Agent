import { useState } from 'react';
import {
  Activity,
  CheckCircle2,
  CircleAlert,
  Loader2,
  Phone,
  Power,
  Save,
  ShieldCheck,
  X,
} from 'lucide-react';
import { api, RuntimeProfile, VoiceAgent } from '@/lib/api';
import styles from './RuntimeControlPanel.module.css';

type Props = {
  agent: VoiceAgent;
  profile: RuntimeProfile;
  onClose: () => void;
  onChange: (profile: RuntimeProfile) => void;
};

export default function RuntimeControlPanel({ agent, profile, onClose, onChange }: Props) {
  const speechProvider: RuntimeProfile['primary_speech_provider'] = agent.voice_provider === 'inworld'
    ? 'inworld'
    : agent.voice_provider === 'elevenlabs' ? 'elevenlabs' : 'sarvam';
  const inworldRuntime = speechProvider === 'inworld';
  const [form, setForm] = useState({
    ...profile,
    telephony_provider: inworldRuntime ? 'livekit_sip' as const : profile.telephony_provider,
    primary_speech_provider: speechProvider,
  });
  const [numbers, setNumbers] = useState(profile.assigned_numbers.join('\n'));
  const [working, setWorking] = useState('');
  const [notice, setNotice] = useState<{ type: 'success' | 'error' | 'info'; text: string } | null>(null);

  const payload = () => ({
    telephony_provider: form.telephony_provider,
    primary_speech_provider: speechProvider,
    fallback_speech_provider: form.fallback_speech_provider,
    llm_provider: form.llm_provider,
    llm_model: form.llm_model,
    stt_language: form.stt_language,
    max_concurrent_calls: Number(form.max_concurrent_calls),
    daily_call_limit: Number(form.daily_call_limit),
    monthly_budget_cents: Number(form.monthly_budget_cents),
    assigned_numbers: numbers.split(/[,\n]/).map((value) => value.trim()).filter(Boolean),
  });
  const persistedPayload = {
    telephony_provider: profile.telephony_provider,
    primary_speech_provider: profile.primary_speech_provider,
    fallback_speech_provider: profile.fallback_speech_provider,
    llm_provider: profile.llm_provider,
    llm_model: profile.llm_model,
    stt_language: profile.stt_language,
    max_concurrent_calls: Number(profile.max_concurrent_calls),
    daily_call_limit: Number(profile.daily_call_limit),
    monthly_budget_cents: Number(profile.monthly_budget_cents),
    assigned_numbers: profile.assigned_numbers,
  };
  const hasUnsavedChanges = JSON.stringify(payload()) !== JSON.stringify(persistedPayload);

  const run = async (action: 'save' | 'test' | 'activate' | 'deactivate') => {
    setWorking(action);
    setNotice(null);
    try {
      let next: RuntimeProfile;
      if (action === 'save') {
        next = await api.updateRuntimeProfile(agent.id, payload());
        setNotice({ type: 'success', text: 'Runtime policy saved. Run readiness before activation.' });
      } else if (action === 'test') {
        if (hasUnsavedChanges) {
          await api.updateRuntimeProfile(agent.id, payload());
        }
        const result = await api.testRuntimeProfile(agent.id);
        next = await api.getRuntimeProfile(agent.id);
        setNotice({
          type: result.ready ? 'success' : 'error',
          text: result.ready
            ? `${hasUnsavedChanges ? 'Runtime policy saved. ' : ''}Every serving dependency passed.`
            : result.blockers.join(' '),
        });
      } else if (action === 'activate') {
        next = await api.activateRuntimeProfile(agent.id);
        setNotice({ type: 'success', text: 'VAV realtime calling is active for this agent.' });
      } else {
        next = await api.deactivateRuntimeProfile(agent.id);
        setNotice({ type: 'info', text: 'Runtime deactivated. Existing provider records were preserved.' });
      }
      onChange(next);
      setForm(next);
      setNumbers(next.assigned_numbers.join('\n'));
    } catch (error) {
      setNotice({
        type: 'error',
        text: error instanceof Error ? error.message : 'Runtime action failed.',
      });
    } finally {
      setWorking('');
    }
  };

  return (
    <section className={styles.panel} aria-labelledby="runtime-panel-title">
      <header className={styles.header}>
        <div className={styles.icon}><Activity size={20} /></div>
        <div>
          <span className="page-kicker">Production serving</span>
          <h2 id="runtime-panel-title">{agent.name} runtime</h2>
          <p>Configure the VAV-owned {inworldRuntime ? 'LiveKit SIP + Inworld speech + selectable response engine' : speechProvider === 'elevenlabs' ? 'ElevenLabs voice and Sarvam transcription' : 'Sarvam speech'} pipeline, capacity, and spend guardrails.</p>
        </div>
        <span className={`badge ${profile.enabled ? 'badge-success' : profile.ready ? 'badge-info' : 'badge-warning'}`}>
          {profile.enabled ? 'Active' : profile.ready ? 'Ready' : profile.status}
        </span>
        <button type="button" className="icon-button" onClick={onClose} aria-label="Close runtime controls"><X size={16} /></button>
      </header>

      {notice ? (
        <div className={`${styles.notice} ${notice.type === 'error' ? styles.error : ''}`} role={notice.type === 'error' ? 'alert' : 'status'}>
          {notice.type === 'success' ? <CheckCircle2 size={15} /> : notice.type === 'error' ? <CircleAlert size={15} /> : <ShieldCheck size={15} />}
          <span>{notice.text}</span>
        </div>
      ) : null}

      <div className={styles.grid}>
        <div className="form-group">
          <label htmlFor="runtime-telephony">Telephony edge</label>
          <select id="runtime-telephony" value={form.telephony_provider} disabled={inworldRuntime} onChange={(event) => setForm({ ...form, telephony_provider: event.target.value as RuntimeProfile['telephony_provider'] })}>
            <option value="twilio">Twilio Media Streams</option>
            <option value="livekit_sip">Etisalat SIP via LiveKit</option>
          </select>
          <p className="form-hint">LiveKit SIP also requires the encrypted trunk credentials in Settings.</p>
        </div>
        <div className="form-group">
          <label htmlFor="runtime-speech-provider">Speech output</label>
          <input
            id="runtime-speech-provider"
            value={inworldRuntime ? 'Inworld STT + TTS 2 (direct)' : speechProvider === 'elevenlabs' ? 'ElevenLabs Flash v2.5' : 'Sarvam Bulbul v3'}
            readOnly
            aria-readonly="true"
          />
          <p className="form-hint">This follows the agent&apos;s selected voice provider. Inworld uses one direct workspace credential for STT and TTS.</p>
        </div>
        <div className="form-group">
          <label htmlFor="runtime-llm">LLM route</label>
          <select id="runtime-llm" value={`${form.llm_provider}:${form.llm_model}`} onChange={(event) => {
            const [llmProvider, ...modelParts] = event.target.value.split(':');
            setForm({
              ...form,
              llm_provider: llmProvider as RuntimeProfile['llm_provider'],
              llm_model: modelParts.join(':'),
            });
          }}>
            {inworldRuntime ? <>
              <optgroup label="Recommended · full VAV knowledge and actions">
                <option value="openai:gpt-4o-mini">OpenAI GPT-4o mini · fast and economical</option>
                <option value="openai:gpt-4o">OpenAI GPT-4o · higher quality</option>
              </optgroup>
              <optgroup label="Inworld Router · requires tool-calling access">
                <option value="inworld:auto">Inworld Router auto</option>
                <option value="inworld:openai/gpt-4o-mini">Inworld Router → GPT-4o mini</option>
                <option value="inworld:openai/gpt-4o">Inworld Router → GPT-4o</option>
              </optgroup>
            </> : <>
              <option value="openai:gpt-4o-mini">OpenAI GPT-4o mini · lower cost</option>
              <option value="openai:gpt-4o">OpenAI GPT-4o · higher quality</option>
            </>}
          </select>
          <p className="form-hint">{inworldRuntime ? 'OpenAI is recommended for reliable VAV knowledge search and actions. Inworld Router remains available only when its workspace can pass the live tool-calling gate.' : 'Direct OpenAI endpoint.'}</p>
        </div>
        <div className="form-group">
          <label htmlFor="runtime-language">Realtime STT language</label>
          <input id="runtime-language" value={form.stt_language} onChange={(event) => setForm({ ...form, stt_language: event.target.value })} placeholder="auto, en-GB, ar-AE, or hi-IN" />
        </div>
        <div className="form-group">
          <label htmlFor="runtime-numbers">Assigned phone numbers</label>
          <textarea id="runtime-numbers" rows={3} value={numbers} onChange={(event) => setNumbers(event.target.value)} placeholder="+971501234567\n+919876543210" />
          <p className="form-hint">One E.164 number per line. The first number is the default outbound caller ID.</p>
        </div>
        <div className="form-group">
          <label htmlFor="runtime-concurrency">Concurrent calls</label>
          <input id="runtime-concurrency" type="number" min={1} max={100} value={form.max_concurrent_calls} onChange={(event) => setForm({ ...form, max_concurrent_calls: Number(event.target.value) })} />
        </div>
        <div className="form-group">
          <label htmlFor="runtime-daily">Daily call limit</label>
          <input id="runtime-daily" type="number" min={1} value={form.daily_call_limit} onChange={(event) => setForm({ ...form, daily_call_limit: Number(event.target.value) })} />
        </div>
        <div className="form-group">
          <label htmlFor="runtime-budget">Monthly budget (USD)</label>
          <input id="runtime-budget" type="number" min={1} step="0.01" value={(form.monthly_budget_cents / 100).toFixed(2)} onChange={(event) => setForm({ ...form, monthly_budget_cents: Math.round(Number(event.target.value) * 100) })} />
        </div>
      </div>

      <div className={styles.readiness}>
        <div><strong>Readiness gates</strong><span>{profile.blockers.length ? `${profile.blockers.length} action${profile.blockers.length === 1 ? '' : 's'} required` : 'All serving dependencies passed'}</span></div>
        {profile.blockers.length ? <ul>{profile.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul> : <CheckCircle2 size={20} />}
      </div>

      <footer className={styles.actions}>
        <button type="button" className="btn btn-secondary" disabled={Boolean(working)} onClick={() => void run('save')}>{working === 'save' ? <Loader2 className="spin" size={14} /> : <Save size={14} />} Save policy</button>
        <button type="button" className="btn btn-secondary" disabled={Boolean(working)} onClick={() => void run('test')}>{working === 'test' ? <Loader2 className="spin" size={14} /> : <Phone size={14} />} {hasUnsavedChanges ? 'Save & test readiness' : 'Test readiness'}</button>
        {profile.enabled ? (
          <button type="button" className="btn btn-danger" disabled={Boolean(working)} onClick={() => void run('deactivate')}><Power size={14} /> Deactivate</button>
        ) : (
          <button type="button" className="btn btn-primary" disabled={Boolean(working) || !profile.ready} onClick={() => void run('activate')}><Power size={14} /> Activate runtime</button>
        )}
      </footer>
    </section>
  );
}
