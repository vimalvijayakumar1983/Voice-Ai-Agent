import { useState } from 'react';
import Link from 'next/link';
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
    staff_browser_only: form.staff_browser_only ?? false,
    knowledge_source_mode: form.knowledge_source_mode ?? 'knowledge_base',
    telephony_provider: form.telephony_provider,
    primary_speech_provider: speechProvider,
    fallback_speech_provider: form.fallback_speech_provider,
    llm_provider: form.llm_provider,
    llm_model: form.llm_model,
    voice_runtime: form.voice_runtime,
    knowledge_turn_mode: form.knowledge_turn_mode,
    stt_language: form.stt_language,
    stt_model: form.stt_model,
    tts_delivery_mode: form.tts_delivery_mode,
    inworld_realtime_tts_model: form.inworld_realtime_tts_model,
    diagnostic_recording_mode: form.diagnostic_recording_mode,
    max_concurrent_calls: Number(form.max_concurrent_calls),
    daily_call_limit: Number(form.daily_call_limit),
    monthly_budget_cents: Number(form.monthly_budget_cents),
    assigned_numbers: numbers.split(/[,\n]/).map((value) => value.trim()).filter(Boolean),
  });
  const persistedPayload = {
    staff_browser_only: profile.staff_browser_only ?? false,
    knowledge_source_mode: profile.knowledge_source_mode ?? 'knowledge_base',
    telephony_provider: profile.telephony_provider,
    primary_speech_provider: profile.primary_speech_provider,
    fallback_speech_provider: profile.fallback_speech_provider,
    llm_provider: profile.llm_provider,
    llm_model: profile.llm_model,
    voice_runtime: profile.voice_runtime,
    knowledge_turn_mode: profile.knowledge_turn_mode,
    stt_language: profile.stt_language,
    stt_model: profile.stt_model,
    tts_delivery_mode: profile.tts_delivery_mode,
    inworld_realtime_tts_model: profile.inworld_realtime_tts_model,
    diagnostic_recording_mode: profile.diagnostic_recording_mode,
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
        setNotice({ type: 'success', text: next.staff_browser_only ? 'Staff browser policy saved. Open the browser test to check the serving dependencies.' : 'Runtime policy saved. Run readiness before activation.' });
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
          <p>Configure the VAV-owned {inworldRuntime ? 'LiveKit SIP + Inworld voice runtime' : speechProvider === 'elevenlabs' ? 'ElevenLabs voice and Sarvam transcription' : 'Sarvam speech'} pipeline, capacity, and spend guardrails.</p>
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
        {inworldRuntime ? <div className="form-group">
          <label htmlFor="runtime-staff-browser">Browser access policy</label>
          <select id="runtime-staff-browser" value={form.staff_browser_only ? 'staff' : 'standard'} onChange={(event) => {
            const staff = event.target.value === 'staff';
            if (staff && numbers.trim()) {
              setNotice({ type: 'error', text: 'Remove assigned phone numbers before selecting staff browser-only mode.' });
              return;
            }
            setForm({ ...form, staff_browser_only: staff,
              knowledge_source_mode: staff ? form.knowledge_source_mode : 'knowledge_base',
              ...(staff ? { voice_runtime: 'inworld_realtime' as const, knowledge_turn_mode: 'tool_loop' as const,
                llm_provider: 'inworld' as const, llm_model: 'openai/gpt-4o-mini', diagnostic_recording_mode: 'off' as const } : {}),
            });
          }}>
            <option value="standard">Standard workspace browser + separate phone activation</option>
            <option value="staff">Staff browser only · owners and administrators</option>
          </select>
          <p className="form-hint">Staff mode requires a signed-in owner/admin, forbids phone activation and recording, and grants no MCP tools automatically. Private ERP data is not enabled by this setting.</p>
        </div> : null}
        {form.staff_browser_only ? <div className="form-group">
          <label htmlFor="runtime-knowledge-source">Knowledge source</label>
          <select id="runtime-knowledge-source" value={form.knowledge_source_mode} onChange={(event) => setForm({ ...form, knowledge_source_mode: event.target.value as RuntimeProfile['knowledge_source_mode'] })}>
            <option value="knowledge_base">Approved knowledge base + optional MCP tools</option>
            <option value="tools_only">MCP tools only · no knowledge base required</option>
          </select>
          <p className="form-hint">Only explicitly approved tools can answer. With no grants, the agent can test voice but cannot retrieve ERP data.</p>
        </div> : null}
        {inworldRuntime ? (
          <div className="form-group">
            <label htmlFor="runtime-architecture">Voice architecture</label>
            <select id="runtime-architecture" value={form.voice_runtime} onChange={(event) => {
              const voiceRuntime = event.target.value as RuntimeProfile['voice_runtime'];
              setForm({
                ...form,
                voice_runtime: voiceRuntime,
                knowledge_turn_mode: voiceRuntime === 'inworld_realtime'
                  ? form.knowledge_turn_mode
                  : 'tool_loop',
                llm_provider: voiceRuntime === 'inworld_realtime' ? 'inworld' : form.llm_provider,
                llm_model: voiceRuntime === 'inworld_realtime' && (form.llm_provider !== 'inworld' || form.llm_model === 'auto')
                  ? 'openai/gpt-4o-mini'
                  : form.llm_model,
              });
            }}>
              <option value="inworld_realtime">Inworld Realtime · VAV production</option>
              <option value="pipeline">Classic component pipeline · rollback</option>
            </select>
            <p className="form-hint">Uses persistent Inworld speech connections through LiveKit. The selected knowledge policy determines whether VAV or Inworld controls response generation and knowledge-tool calls.</p>
          </div>
        ) : null}
        {inworldRuntime && form.voice_runtime === 'inworld_realtime' ? (
          <div className="form-group">
            <label htmlFor="runtime-knowledge-turn-mode">Knowledge turn policy</label>
            <select
              id="runtime-knowledge-turn-mode"
              value={form.knowledge_turn_mode}
              onChange={(event) => setForm({
                ...form,
                knowledge_turn_mode: event.target.value as RuntimeProfile['knowledge_turn_mode'],
              })}
            >
              <option value="tool_loop">Provider tool loop · previous mode</option>
              <option value="single_pass_experimental">VAV grounded single pass · production baseline</option>
            </select>
            <p className="form-hint">The production baseline uses VAV’s approved knowledge retrieval and conversation handling with Inworld speech. The previous mode lets Inworld invoke knowledge tools.</p>
            {form.knowledge_turn_mode === 'single_pass_experimental' ? (
              <p className="form-hint" role="note">Applies to every new call for this agent; this is not a per-call split. Save, attach approved knowledge and test in the browser. Phone activation separately requires SIP readiness. Latency improvements remain under evaluation.</p>
            ) : null}
          </div>
        ) : null}
        <div className="form-group">
          <label htmlFor="runtime-telephony">Telephony edge</label>
          <select id="runtime-telephony" value={form.telephony_provider} disabled={inworldRuntime} onChange={(event) => {
            const telephonyProvider = event.target.value as RuntimeProfile['telephony_provider'];
            setForm({
              ...form,
              telephony_provider: telephonyProvider,
              diagnostic_recording_mode: telephonyProvider === 'livekit_sip'
                ? form.diagnostic_recording_mode
                : 'off',
            });
          }}>
            <option value="twilio">Twilio Media Streams</option>
            <option value="livekit_sip">Etisalat SIP via LiveKit</option>
          </select>
          <p className="form-hint">LiveKit SIP also requires the encrypted trunk credentials in Settings.</p>
        </div>
        <div className="form-group">
          <label htmlFor="runtime-diagnostic-recording">Diagnostic call audio policy</label>
          <select
            id="runtime-diagnostic-recording"
            value={form.diagnostic_recording_mode}
            onChange={(event) => setForm({
              ...form,
              diagnostic_recording_mode: event.target.value as RuntimeProfile['diagnostic_recording_mode'],
            })}
          >
            <option value="off">Off · safe default</option>
            <option value="livekit_egress_explicit_consent" disabled={form.telephony_provider !== 'livekit_sip'}>
              Request LiveKit diagnostic capture · explicit consent
            </option>
          </select>
          <p className="form-hint">This saves an opt-in policy request only. It does not start LiveKit Egress, store audio, or make VAV playback available.</p>
          {form.diagnostic_recording_mode !== 'off' ? (
            <p className="form-hint" role="note">Activation remains blocked until every call requires explicit recording consent—absence is not consent—and LiveKit Egress, encrypted regional storage, retention, deletion, and access auditing are verified.</p>
          ) : null}
        </div>
        <div className="form-group">
          <label htmlFor="runtime-speech-provider">Speech output</label>
          <input
            id="runtime-speech-provider"
            value={inworldRuntime ? (form.voice_runtime === 'inworld_realtime' ? `Inworld Realtime + ${form.inworld_realtime_tts_model}` : 'Inworld STT + TTS-2 components') : speechProvider === 'elevenlabs' ? 'ElevenLabs Flash v2.5' : 'Sarvam Bulbul v3'}
            readOnly
            aria-readonly="true"
          />
          <p className="form-hint">This follows the agent&apos;s selected voice provider. Native Realtime uses the same Inworld workspace credential for the complete session.</p>
        </div>
        {inworldRuntime && form.voice_runtime === 'inworld_realtime' ? (
          <div className="form-group">
            <label htmlFor="runtime-realtime-tts-model">Realtime voice engine</label>
            <select id="runtime-realtime-tts-model" value={form.inworld_realtime_tts_model} onChange={(event) => setForm({ ...form, inworld_realtime_tts_model: event.target.value as RuntimeProfile['inworld_realtime_tts_model'] })}>
              <option value="inworld-tts-1.5-max">TTS 1.5 Max · recommended quality and speed</option>
              <option value="inworld-tts-1.5-mini">TTS 1.5 Mini · lowest latency</option>
              <option value="inworld-tts-2">TTS-2 · expressive rollback option</option>
            </select>
            <p className="form-hint">Saved per agent, verified by readiness, and stamped into every call trace. Start with Max; use Mini only when the latency gate requires it.</p>
          </div>
        ) : null}
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
            {inworldRuntime && form.voice_runtime === 'inworld_realtime' ? <>
              <optgroup label="Inside the native Inworld Realtime session">
                <option value="inworld:openai/gpt-4o-mini">Inworld Router → GPT-4o mini</option>
                <option value="inworld:openai/gpt-4o">Inworld Router → GPT-4o</option>
              </optgroup>
            </> : inworldRuntime ? <>
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
          <p className="form-hint">{inworldRuntime && form.voice_runtime === 'inworld_realtime' ? 'The selected model runs inside Inworld Realtime; no separate VAV-to-OpenAI request is made. Readiness verifies the exact model, voice, and transcription route.' : inworldRuntime ? 'OpenAI is recommended for reliable VAV knowledge search and actions. Inworld Router remains available only when its workspace can pass the live tool-calling gate.' : 'Direct OpenAI endpoint.'}</p>
        </div>
        <div className="form-group">
          <label htmlFor="runtime-language">Realtime STT language</label>
          <input id="runtime-language" value={form.stt_language} onChange={(event) => setForm({ ...form, stt_language: event.target.value })} placeholder="auto, en-GB, ar-AE, or hi-IN" />
        </div>
        {inworldRuntime ? (
          <div className="form-group">
            <label htmlFor="runtime-stt-model">Speech recognition model</label>
            <select id="runtime-stt-model" value={form.stt_model} onChange={(event) => setForm({ ...form, stt_model: event.target.value as RuntimeProfile['stt_model'] })}>
              <option value="auto">Automatic · best model for configured languages</option>
              <option value="assemblyai/u3-rt-pro">AssemblyAI U3 Pro · fast, accurate English/European</option>
              <option value="soniox/stt-rt-v4">Soniox RT v4 · multilingual including Arabic and Hindi</option>
              <option value="inworld/inworld-stt-1">Inworld STT 1 · experimental first-party</option>
            </select>
            <p className="form-hint">Automatic uses U3 Pro for English and supported European languages, and Soniox for wider multilingual coverage. Native mode also sends a business-name transcription hint.</p>
          </div>
        ) : null}
        {inworldRuntime && form.voice_runtime === 'pipeline' ? (
          <div className="form-group">
            <label htmlFor="runtime-delivery-mode">Inworld TTS delivery</label>
            <select id="runtime-delivery-mode" value={form.tts_delivery_mode} onChange={(event) => setForm({ ...form, tts_delivery_mode: event.target.value as RuntimeProfile['tts_delivery_mode'] })}>
              <option value="balanced">Balanced · recommended default</option>
              <option value="creative">Creative · more expressive</option>
              <option value="stable">Stable · most predictable</option>
            </select>
            <p className="form-hint">Uses Inworld TTS-2&apos;s native delivery mode. It has no separate feature fee; normal synthesized-character usage still applies.</p>
            <p className="form-hint">LiveKit native dynamic turn detection, interruption recovery, and standard noise cancellation are applied automatically. Transport, SIP, and model usage remain billable.</p>
          </div>
        ) : null}
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
        <div><strong>{profile.staff_browser_only ? 'Browser testing' : 'Phone readiness gates'}</strong></div>
        {profile.staff_browser_only ? <p>Phone activation is intentionally disabled. Browser start checks Inworld, LiveKit worker, your current staff role, capacity and budget. An MCP-only agent does not require a knowledge base. Passing a browser test does not enable ERP permissions.</p> : profile.blockers.length ? <ul>{profile.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul> : <CheckCircle2 size={20} />}
      </div>

      <footer className={styles.actions}>
        <button type="button" className="btn btn-secondary" disabled={Boolean(working)} onClick={() => void run('save')}>{working === 'save' ? <Loader2 className="spin" size={14} /> : <Save size={14} />} Save policy</button>
        {profile.staff_browser_only ? (!hasUnsavedChanges && <Link className="btn btn-primary" href={`/playground?agent=${agent.id}`}>Open browser test</Link>) : <button type="button" className="btn btn-secondary" disabled={Boolean(working)} onClick={() => void run('test')}>{working === 'test' ? <Loader2 className="spin" size={14} /> : <Phone size={14} />} {hasUnsavedChanges ? 'Save & test readiness' : 'Test readiness'}</button>}
        {profile.staff_browser_only ? (profile.status !== 'inactive' && <button type="button" className="btn btn-danger" disabled={Boolean(working)} onClick={() => void run('deactivate')}><Power size={14} /> Disable browser access</button>) : profile.enabled ? (
          <button type="button" className="btn btn-danger" disabled={Boolean(working)} onClick={() => void run('deactivate')}><Power size={14} /> Deactivate</button>
        ) : (
          <button type="button" className="btn btn-primary" disabled={Boolean(working) || !profile.ready} onClick={() => void run('activate')}><Power size={14} /> Activate runtime</button>
        )}
      </footer>
    </section>
  );
}
