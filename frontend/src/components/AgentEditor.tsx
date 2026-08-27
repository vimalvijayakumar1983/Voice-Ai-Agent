import { FormEvent, useMemo, useState } from 'react';
import { Languages, Search, Sparkles, Volume2 } from 'lucide-react';
import {
  AgentProviderCatalog,
  AgentTemplate,
  LanguageCatalogItem,
  VoiceCatalogItem,
} from '@/lib/api';

export interface AgentEditorValues {
  name: string;
  description: string;
  system_prompt: string;
  greeting_message: string;
  model_provider: string;
  model_name: string;
  voice_provider: string;
  voice_id: string;
  temperature: number;
  language: string;
  supported_languages: string[];
  speech_rate: number;
  timezone: string;
}

export const defaultAgentValues: AgentEditorValues = {
  name: '',
  description: '',
  system_prompt: '',
  greeting_message: '',
  model_provider: 'smallest',
  model_name: 'electron',
  voice_provider: 'smallest',
  voice_id: '',
  temperature: 0.7,
  language: 'en',
  supported_languages: ['en'],
  speech_rate: 1,
  timezone: 'Asia/Dubai',
};

const fallbackLanguages: LanguageCatalogItem[] = [
  ['bn', 'Bengali'], ['nl', 'Dutch'], ['en', 'English'], ['fr', 'French'],
  ['de', 'German'], ['gu', 'Gujarati'], ['hi', 'Hindi'], ['it', 'Italian'],
  ['kn', 'Kannada'], ['ml', 'Malayalam'], ['mr', 'Marathi'], ['or', 'Odia'],
  ['pl', 'Polish'], ['pt', 'Portuguese'], ['pa', 'Punjabi'], ['ru', 'Russian'],
  ['es', 'Spanish'], ['sv', 'Swedish'], ['ta', 'Tamil'], ['te', 'Telugu'],
].map(([code, name]) => ({ code, name }));

const noVoices: VoiceCatalogItem[] = [];

interface AgentEditorProps {
  mode: 'create' | 'edit';
  catalog: AgentProviderCatalog | null;
  initialValues?: AgentEditorValues;
  busy?: boolean;
  onCancel: () => void;
  onSubmit: (values: AgentEditorValues) => Promise<void>;
}

export default function AgentEditor({
  mode,
  catalog,
  initialValues = defaultAgentValues,
  busy = false,
  onCancel,
  onSubmit,
}: AgentEditorProps) {
  const [form, setForm] = useState<AgentEditorValues>(initialValues);
  const [voiceSearch, setVoiceSearch] = useState('');
  const [showAllVoices, setShowAllVoices] = useState(false);
  const languages = catalog?.languages.length ? catalog.languages : fallbackLanguages;
  const voices = catalog?.voices ?? noVoices;

  const visibleVoices = useMemo(() => {
    const query = voiceSearch.trim().toLowerCase();
    return voices.filter((voice) => {
      const matchesLanguage = showAllVoices || voice.languages.includes(form.language);
      const searchable = [voice.name, voice.id, voice.accent, voice.gender, ...voice.languages]
        .filter(Boolean)
        .join(' ')
        .toLowerCase();
      return matchesLanguage && (!query || searchable.includes(query));
    });
  }, [form.language, showAllVoices, voiceSearch, voices]);

  const selectedVoice = voices.find((voice) => voice.id === form.voice_id);

  const updatePrimaryLanguage = (language: string) => {
    setForm((current) => ({
      ...current,
      language,
      supported_languages: language === 'ta'
        ? ['ta']
        : current.supported_languages.includes('ta')
          ? [language]
          : Array.from(new Set([...current.supported_languages, language])),
    }));
  };

  const toggleSupportedLanguage = (language: string) => {
    setForm((current) => {
      const selected = current.supported_languages.includes(language);
      if (selected && current.language === language) return current;
      if (selected) {
        return {
          ...current,
          supported_languages: current.supported_languages.filter((code) => code !== language),
        };
      }
      if (language === 'ta') {
        return { ...current, language: 'ta', supported_languages: ['ta'] };
      }
      if (current.supported_languages.includes('ta')) {
        return { ...current, language, supported_languages: [language] };
      }
      return {
        ...current,
        supported_languages: [...current.supported_languages, language],
      };
    });
  };

  const applyTemplate = (template: AgentTemplate) => {
    setForm((current) => ({
      ...current,
      name: template.name,
      description: template.description,
      system_prompt: template.system_prompt,
      greeting_message: template.greeting_message,
      language: template.default_language,
      supported_languages: template.supported_languages,
      voice_id: template.voice_id,
      speech_rate: template.speech_rate,
      temperature: template.temperature,
      timezone: template.timezone,
    }));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    await onSubmit(form);
  };

  return (
    <form className="agent-editor" onSubmit={submit}>
      {mode === 'create' && (
        <section className="editor-section">
          <div className="editor-section-heading">
            <div><span className="section-icon"><Sparkles size={14} /></span><h3>Start from a proven template</h3></div>
            <p>Every field stays editable after you choose one.</p>
          </div>
          <div className="template-grid">
            {(catalog?.templates ?? []).map((template) => (
              <button type="button" className="template-card" key={template.id} onClick={() => applyTemplate(template)}>
                <span>{template.category}</span>
                <strong>{template.name}</strong>
                <p>{template.description}</p>
              </button>
            ))}
            {!catalog?.templates.length && (
              <p className="catalog-empty">Templates will appear when the Smallest.ai catalog is available.</p>
            )}
          </div>
        </section>
      )}

      <section className="editor-section">
        <div className="editor-section-heading">
          <div><span className="section-icon"><Sparkles size={14} /></span><h3>Identity & behavior</h3></div>
        </div>
        <div className="form-grid">
          <div className="form-group">
            <label>Agent name</label>
            <input required value={form.name} placeholder="e.g. Customer Care Concierge" onChange={(event) => setForm({ ...form, name: event.target.value })} />
          </div>
          <div className="form-group">
            <label>Purpose</label>
            <input value={form.description} placeholder="Appointments, sales, support…" onChange={(event) => setForm({ ...form, description: event.target.value })} />
          </div>
        </div>
        <div className="form-group">
          <label>System prompt <span>{form.system_prompt.length} characters</span></label>
          <textarea required minLength={10} maxLength={4000} value={form.system_prompt} placeholder="Define the role, goals, conversation flow, escalation path, and guardrails…" onChange={(event) => setForm({ ...form, system_prompt: event.target.value })} />
          <p className="form-hint">Keep responses spoken and concise. Confirm important information and never invent customer data.</p>
        </div>
        <div className="form-group">
          <label>First message</label>
          <input value={form.greeting_message} placeholder="Hello, how can I help you today?" onChange={(event) => setForm({ ...form, greeting_message: event.target.value })} />
        </div>
      </section>

      <section className="editor-section">
        <div className="editor-section-heading">
          <div><span className="section-icon"><Languages size={14} /></span><h3>Languages</h3></div>
          <p>{form.supported_languages.length} selected · the agent starts in the primary language</p>
        </div>
        <div className="form-group language-primary">
          <label>Primary language</label>
          <select value={form.language} onChange={(event) => updatePrimaryLanguage(event.target.value)}>
            {languages.map((language) => <option value={language.code} key={language.code}>{language.name}</option>)}
          </select>
        </div>
        <div className="language-grid" role="group" aria-label="Supported languages">
          {languages.map((language) => {
            const selected = form.supported_languages.includes(language.code);
            const primary = form.language === language.code;
            return (
              <button
                type="button"
                className={`language-option ${selected ? 'selected' : ''}`}
                aria-pressed={selected}
                key={language.code}
                onClick={() => toggleSupportedLanguage(language.code)}
              >
                <span>{language.name}</span><small>{primary ? 'Primary' : language.code.toUpperCase()}</small>
              </button>
            );
          })}
        </div>
        {form.supported_languages.includes('ta') && (
          <p className="form-hint">Smallest.ai currently requires Tamil agents to use Tamil as their only language.</p>
        )}
      </section>

      <section className="editor-section">
        <div className="editor-section-heading">
          <div><span className="section-icon"><Volume2 size={14} /></span><h3>Voice</h3></div>
          <p>{voices.length} Smallest.ai voices available</p>
        </div>
        <div className="voice-toolbar">
          <label className="voice-search"><Search size={14} /><input value={voiceSearch} placeholder="Search voice, ID, accent…" onChange={(event) => setVoiceSearch(event.target.value)} /></label>
          <label className="checkbox-control"><input type="checkbox" checked={showAllVoices} onChange={(event) => setShowAllVoices(event.target.checked)} /> Show every language</label>
        </div>
        <div className="form-group">
          <label>Smallest.ai voice <span>{visibleVoices.length} matches</span></label>
          <select value={form.voice_id} onChange={(event) => setForm({ ...form, voice_id: event.target.value })}>
            <option value="">Platform default voice</option>
            {form.voice_id && !visibleVoices.some((voice) => voice.id === form.voice_id) && (
              <option value={form.voice_id}>{selectedVoice?.name ?? form.voice_id} · Current selection</option>
            )}
            {visibleVoices.map((voice) => <VoiceOption voice={voice} key={voice.id} />)}
          </select>
        </div>
        {selectedVoice && (
          <div className="voice-summary">
            <div className="voice-summary-icon"><Volume2 size={16} /></div>
            <div><strong>{selectedVoice.name}</strong><span>{voiceDescription(selectedVoice)}</span></div>
            <code>{selectedVoice.id}</code>
            {selectedVoice.source === 'cloned' && <span className="badge badge-info">Your clone</span>}
          </div>
        )}
      </section>

      <section className="editor-section editor-section-compact">
        <div className="editor-section-heading">
          <div><span className="section-icon"><Sparkles size={14} /></span><h3>Conversation tuning</h3></div>
        </div>
        <div className="form-grid">
          <div className="form-group">
            <label>Model</label>
            <select value={form.model_name} onChange={(event) => setForm({ ...form, model_name: event.target.value })}><option value="electron">Electron · voice optimized</option></select>
          </div>
          <div className="form-group">
            <label>Timezone</label>
            <input required list="common-timezones" value={form.timezone} onChange={(event) => setForm({ ...form, timezone: event.target.value })} />
            <datalist id="common-timezones"><option value="Asia/Dubai" /><option value="Asia/Kolkata" /><option value="Europe/London" /><option value="America/New_York" /><option value="America/Los_Angeles" /></datalist>
          </div>
          <div className="form-group range-control">
            <label>Speech rate <span>{form.speech_rate.toFixed(2)}×</span></label>
            <input type="range" min="0.5" max="2" step="0.05" value={form.speech_rate} onChange={(event) => setForm({ ...form, speech_rate: Number(event.target.value) })} />
          </div>
          <div className="form-group range-control">
            <label>Creativity <span>{form.temperature.toFixed(1)}</span></label>
            <input type="range" min="0" max="2" step="0.1" value={form.temperature} onChange={(event) => setForm({ ...form, temperature: Number(event.target.value) })} />
          </div>
        </div>
      </section>

      <div className="editor-actions">
        <button type="button" className="btn btn-secondary" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn-primary" disabled={busy}>{busy ? 'Saving…' : mode === 'create' ? 'Create local draft' : 'Save changes'}</button>
      </div>
    </form>
  );
}

function VoiceOption({ voice }: { voice: VoiceCatalogItem }) {
  return <option value={voice.id}>{voice.name} · {voice.languages.join(', ').toUpperCase() || 'Multilingual'}{voice.accent ? ` · ${voice.accent}` : ''}{voice.source === 'cloned' ? ' · Cloned' : ''}</option>;
}

function voiceDescription(voice: VoiceCatalogItem) {
  return [voice.languages.join(', ').toUpperCase(), voice.accent, voice.gender, voice.age]
    .filter(Boolean)
    .join(' · ');
}
