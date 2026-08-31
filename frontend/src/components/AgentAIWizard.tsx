import { FormEvent, useId, useState } from 'react';
import { Loader2, ShieldCheck, Sparkles } from 'lucide-react';
import { AgentAIDraftRequest, AgentProviderCatalog } from '@/lib/api';

interface AgentAIWizardProps {
  catalog: AgentProviderCatalog;
  busy: boolean;
  onCancel: () => void;
  onGenerate: (request: AgentAIDraftRequest) => Promise<void>;
}

export default function AgentAIWizard({ catalog, busy, onCancel, onGenerate }: AgentAIWizardProps) {
  const [brief, setBrief] = useState('');
  const [provider, setProvider] = useState<AgentAIDraftRequest['provider_preference']>('auto');
  const [language, setLanguage] = useState('en');
  const [timezone, setTimezone] = useState('Asia/Dubai');
  const briefId = useId();
  const providerId = useId();
  const languageId = useId();
  const timezoneId = useId();
  const availableProviders = new Set(catalog.voices.map((voice) => voice.provider));

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    await onGenerate({
      brief: brief.trim(),
      provider_preference: provider,
      primary_language: language,
      timezone,
    });
  };

  return (
    <form className="agent-editor" onSubmit={submit}>
      <section className="editor-section">
        <div className="editor-section-heading">
          <div><span className="section-icon"><Sparkles size={14} /></span><h3>Describe the agent you need</h3></div>
          <p>OpenAI produces a draft only. You review every field before anything is saved.</p>
        </div>
        <div className="form-group">
          <label htmlFor={briefId}>Business and agent brief <span>{brief.length}/4000</span></label>
          <textarea
            id={briefId}
            required
            minLength={20}
            maxLength={4000}
            rows={9}
            value={brief}
            placeholder="Example: Create an inbound receptionist for Adam & Eve Cosmetic Medical Centre. Answer only from the approved knowledge base, explain treatments, capture appointment requests, and escalate medical or pricing questions that cannot be verified."
            onChange={(event) => setBrief(event.target.value)}
          />
          <p className="form-hint">Include the business, callers, tasks, restrictions, escalation path, connected actions, and desired tone. This description is sent to your configured OpenAI workspace.</p>
        </div>
      </section>

      <section className="editor-section editor-section-compact">
        <div className="editor-section-heading">
          <div><span className="section-icon"><ShieldCheck size={14} /></span><h3>Capability boundaries</h3></div>
          <p>VAV constrains the result to the live provider and language catalog.</p>
        </div>
        <div className="form-grid">
          <div className="form-group">
            <label htmlFor={providerId}>Voice provider</label>
            <select id={providerId} value={provider} onChange={(event) => setProvider(event.target.value as AgentAIDraftRequest['provider_preference'])}>
              <option value="auto">Choose the best available provider</option>
              {availableProviders.has('smallest') ? <option value="smallest">Smallest.ai</option> : null}
              {availableProviders.has('sarvam') ? <option value="sarvam">Sarvam AI</option> : null}
              {availableProviders.has('elevenlabs') ? <option value="elevenlabs">ElevenLabs</option> : null}
            </select>
          </div>
          <div className="form-group">
            <label htmlFor={languageId}>Primary language</label>
            <select id={languageId} value={language} onChange={(event) => setLanguage(event.target.value)}>
              {catalog.languages.map((item) => <option value={item.code} key={item.code}>{item.name}</option>)}
            </select>
          </div>
          <div className="form-group">
            <label htmlFor={timezoneId}>Timezone</label>
            <input id={timezoneId} required list="ai-wizard-timezones" value={timezone} onChange={(event) => setTimezone(event.target.value)} />
            <datalist id="ai-wizard-timezones"><option value="Asia/Dubai" /><option value="Asia/Kolkata" /><option value="Europe/London" /><option value="America/New_York" /></datalist>
          </div>
        </div>
      </section>

      <div className="editor-actions">
        <button type="button" className="btn btn-secondary" disabled={busy} onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn-primary" disabled={busy || brief.trim().length < 20}>
          {busy ? <><Loader2 className="spin" size={14} /> Generating draft…</> : <><Sparkles size={14} /> Generate reviewable draft</>}
        </button>
      </div>
    </form>
  );
}
