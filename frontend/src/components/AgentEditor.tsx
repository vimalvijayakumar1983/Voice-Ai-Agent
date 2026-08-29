import { FormEvent, useId, useMemo, useState } from 'react';
import { CheckCircle2, Languages, LockKeyhole, Sparkles, Volume2 } from 'lucide-react';
import {
  AgentProviderCatalog,
  AgentTemplate,
  LanguageCatalogItem,
} from '@/lib/api';
import VoiceLibrary from './VoiceLibrary';
import {
  reconcileVoiceSelection,
  voiceCompatibility,
  voiceConfigurationGuard,
} from './voice-library.cjs';
import {
  languageSwitchingState,
  normalizeLanguageSwitching,
  switchingStatus,
  withValidSwitching,
} from './language-switching.cjs';
import styles from './AgentEditor.module.css';

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
  language_switching_enabled: boolean;
  language_switching_mode: 'disabled' | 'automatic';
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
  language_switching_enabled: false,
  language_switching_mode: 'disabled',
  speech_rate: 1,
  timezone: 'Asia/Dubai',
};

const fallbackLanguages: LanguageCatalogItem[] = [
  ['ar', 'Arabic'], ['bn', 'Bengali'], ['nl', 'Dutch'], ['en', 'English'], ['fr', 'French'],
  ['de', 'German'], ['gu', 'Gujarati'], ['hi', 'Hindi'], ['it', 'Italian'],
  ['kn', 'Kannada'], ['ml', 'Malayalam'], ['mr', 'Marathi'], ['or', 'Odia'],
  ['pl', 'Polish'], ['pt', 'Portuguese'], ['pa', 'Punjabi'], ['ru', 'Russian'],
  ['es', 'Spanish'], ['sv', 'Swedish'], ['ta', 'Tamil'], ['te', 'Telugu'],
].map(([code, name]) => ({ code, name }));

interface AgentEditorProps {
  mode: 'create' | 'edit';
  catalog: AgentProviderCatalog | null;
  initialValues?: AgentEditorValues;
  catalogError?: string | null;
  busy?: boolean;
  onCancel: () => void;
  onSubmit: (values: AgentEditorValues) => Promise<void>;
  onCatalogRefresh: () => Promise<void>;
}

export default function AgentEditor({
  mode,
  catalog,
  initialValues = defaultAgentValues,
  catalogError = null,
  busy = false,
  onCancel,
  onSubmit,
  onCatalogRefresh,
}: AgentEditorProps) {
  const [originalConfiguration] = useState<AgentEditorValues>(() => normalizeLanguageSwitching(initialValues));
  const [form, setForm] = useState<AgentEditorValues>(originalConfiguration);
  const [voiceNotice, setVoiceNotice] = useState<string | null>(null);
  const nameId = useId();
  const purposeId = useId();
  const promptId = useId();
  const greetingId = useId();
  const primaryLanguageId = useId();
  const modelId = useId();
  const timezoneId = useId();
  const speechRateId = useId();
  const languages = catalog?.languages.length ? catalog.languages : fallbackLanguages;
  const voices = useMemo(() => catalog?.voices ?? [], [catalog?.voices]);
  const catalogUsable = Boolean(catalog && !catalogError && voices.length > 0 && catalog.languages.length > 0);
  const configurationGuard = voiceConfigurationGuard({
    editorMode: mode,
    catalogAvailable: catalogUsable,
    original: originalConfiguration,
    current: form,
  });
  const storedConfigurationPreserved = mode === 'edit' && !configurationGuard.changed;

  const compatibleVoiceCount = useMemo(
    () => voices.filter((voice) => voiceCompatibility(voice, form.supported_languages).status === 'compatible').length,
    [form.supported_languages, voices],
  );
  const currentVoice = voices.find((voice) => voice.id === form.voice_id);
  const currentVoiceCompatibility = form.voice_id && catalog
    ? voiceCompatibility(currentVoice, form.supported_languages)
    : null;
  const effectiveVoiceId = currentVoiceCompatibility?.status === 'compatible' ? form.voice_id : '';
  const effectiveVoiceNotice = voiceNotice ?? (
    currentVoiceCompatibility && currentVoiceCompatibility.status !== 'compatible'
      ? storedConfigurationPreserved
        ? `${currentVoice?.name ?? form.voice_id} cannot be revalidated against the current catalog. It will be preserved for unrelated edits; choose a replacement before changing voice or languages.`
        : `${currentVoice?.name ?? form.voice_id} is not verified for every selected language. Choose a compatible voice before saving.`
      : null
  );

  const applyLanguageConfiguration = (next: AgentEditorValues, previousLanguageCount: number) => {
    if (!catalogUsable) {
      setVoiceNotice(configurationGuard.reason);
      return;
    }
    const switched = withValidSwitching(next, previousLanguageCount);
    const selection = catalog
      ? reconcileVoiceSelection(switched.voice_id, voices, switched.supported_languages)
      : null;
    if (selection?.removed && selection.compatibility) {
      setForm({ ...switched, voice_id: selection.voiceId });
      setVoiceNotice(
        `${selection.voice?.name ?? switched.voice_id} was removed because it is ${selection.compatibility.status === 'incompatible' ? 'missing one or more selected languages' : 'not verified for this configuration'}. Choose a compatible voice below.`,
      );
      return;
    }
    setForm(switched);
    setVoiceNotice(null);
  };

  const updatePrimaryLanguage = (language: string) => {
    const supportedLanguages = Array.from(new Set([...form.supported_languages, language]));
    applyLanguageConfiguration(
      { ...form, language, supported_languages: supportedLanguages },
      form.supported_languages.length,
    );
  };

  const toggleSupportedLanguage = (language: string) => {
    const selected = form.supported_languages.includes(language);
    if (selected && form.language === language) return;
    if (selected) {
      applyLanguageConfiguration({
        ...form,
        supported_languages: form.supported_languages.filter((code) => code !== language),
      }, form.supported_languages.length);
      return;
    }
    applyLanguageConfiguration({
      ...form,
      supported_languages: [...form.supported_languages, language],
    }, form.supported_languages.length);
  };

  const updateSwitchingMode = (mode: AgentEditorValues['language_switching_mode']) => {
    if (!catalogUsable) {
      setVoiceNotice(configurationGuard.reason);
      return;
    }
    setForm((current) => ({
      ...current,
      language_switching_mode: mode,
      language_switching_enabled: mode === 'automatic',
    }));
  };

  const applyTemplate = (template: AgentTemplate) => {
    const switching = languageSwitchingState(template.supported_languages);
    applyLanguageConfiguration({
      ...form,
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
      ...switching,
    }, form.supported_languages.length);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const guard = voiceConfigurationGuard({
      editorMode: mode,
      catalogAvailable: catalogUsable,
      original: originalConfiguration,
      current: form,
    });
    if (!guard.allowed) {
      setVoiceNotice(guard.reason);
      return;
    }
    if (mode === 'edit' && !guard.changed) {
      await onSubmit(form);
      return;
    }
    const selectedVoice = voices.find((voice) => voice.id === form.voice_id);
    const selectedCompatibility = form.voice_id
      ? voiceCompatibility(selectedVoice, form.supported_languages)
      : null;
    if (selectedCompatibility && selectedCompatibility.status !== 'compatible') {
      setForm((current) => ({ ...current, voice_id: '' }));
      setVoiceNotice('The previous voice is no longer verified for this configuration. Choose a compatible voice before saving.');
      return;
    }
    if (form.supported_languages.length > 1 && !selectedVoice) {
      setVoiceNotice('Choose a voice verified for every selected language before saving a multilingual agent.');
      return;
    }
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
            <label htmlFor={nameId}>Agent name</label>
            <input id={nameId} required value={form.name} placeholder="e.g. Customer Care Concierge" onChange={(event) => setForm({ ...form, name: event.target.value })} />
          </div>
          <div className="form-group">
            <label htmlFor={purposeId}>Purpose</label>
            <input id={purposeId} value={form.description} placeholder="Appointments, sales, support…" onChange={(event) => setForm({ ...form, description: event.target.value })} />
          </div>
        </div>
        <div className="form-group">
          <label htmlFor={promptId}>System prompt <span>{form.system_prompt.length} characters</span></label>
          <textarea id={promptId} required minLength={10} maxLength={4000} value={form.system_prompt} placeholder="Define the role, goals, conversation flow, escalation path, and guardrails…" onChange={(event) => setForm({ ...form, system_prompt: event.target.value })} />
          <p className="form-hint">Keep responses spoken and concise. Confirm important information and never invent customer data.</p>
        </div>
        <div className="form-group">
          <label htmlFor={greetingId}>First message</label>
          <input id={greetingId} value={form.greeting_message} placeholder="Hello, how can I help you today?" onChange={(event) => setForm({ ...form, greeting_message: event.target.value })} />
        </div>
      </section>

      <section className="editor-section">
        <div className="editor-section-heading">
          <div><span className="section-icon"><Languages size={14} /></span><h3>Languages</h3></div>
          <p>{form.supported_languages.length} selected · the agent starts in the primary language</p>
        </div>
        <div className="form-group language-primary">
          <label htmlFor={primaryLanguageId}>Primary language</label>
          <select id={primaryLanguageId} value={form.language} disabled={!catalogUsable} onChange={(event) => updatePrimaryLanguage(event.target.value)}>
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
                className={`language-option ${selected ? 'selected' : ''} ${!catalogUsable ? styles.languageOptionLocked : ''}`}
                aria-pressed={selected}
                aria-label={`${selected ? 'Remove' : 'Add'} ${language.name}${primary ? ', primary language' : ''}`}
                disabled={!catalogUsable}
                key={language.code}
                onClick={() => toggleSupportedLanguage(language.code)}
              >
                <span>{language.name}</span><small>{primary ? 'Primary' : language.code.toUpperCase()}</small>
              </button>
            );
          })}
        </div>
        {!catalogUsable && (
          <div className={styles.catalogLock} role="status">
            <LockKeyhole size={15} aria-hidden="true" />
            <span>{mode === 'edit'
              ? 'The voice catalog is unavailable, so the stored voice, languages, and switching mode are locked. You can still save unrelated agent changes.'
              : 'Connect to the Smallest.ai voice catalog before choosing languages or creating this agent.'}</span>
          </div>
        )}
        <fieldset className={styles.switchingFieldset}>
          <legend>Same-call language behavior</legend>
          <div className={styles.switchingOptions}>
            <label className={`${styles.switchingOption} ${form.language_switching_mode === 'disabled' ? styles.switchingOptionSelected : ''} ${!catalogUsable ? styles.switchingOptionDisabled : ''}`}>
              <input
                type="radio"
                name="language-switching-mode"
                value="disabled"
                checked={form.language_switching_mode === 'disabled'}
                disabled={!catalogUsable}
                onChange={() => updateSwitchingMode('disabled')}
              />
              <span>
                <strong>Fixed language per call</strong>
                <small>The agent stays in the primary language for the entire conversation.</small>
              </span>
            </label>
            <label className={`${styles.switchingOption} ${form.language_switching_mode === 'automatic' ? styles.switchingOptionSelected : ''} ${!catalogUsable || form.supported_languages.length < 2 ? styles.switchingOptionDisabled : ''}`}>
              <input
                type="radio"
                name="language-switching-mode"
                value="automatic"
                checked={form.language_switching_mode === 'automatic'}
                disabled={!catalogUsable || form.supported_languages.length < 2}
                onChange={() => updateSwitchingMode('automatic')}
              />
              <span>
                <strong>Automatic same-call switching</strong>
                <small>Smallest.ai can detect a caller changing languages when the exact combination is supported.</small>
              </span>
            </label>
          </div>
          <div className={`${styles.switchingStatus} ${form.language_switching_enabled ? styles.switchingStatusReady : ''}`} role="status">
            {form.language_switching_enabled ? <CheckCircle2 size={15} aria-hidden="true" /> : <Languages size={15} aria-hidden="true" />}
            <span>{!catalogUsable && mode === 'edit'
              ? 'Stored same-call behavior is preserved until the catalog is available for validation.'
              : switchingStatus(form)}</span>
          </div>
        </fieldset>
      </section>

      <section className="editor-section">
        <div className="editor-section-heading">
          <div><span className="section-icon"><Volume2 size={14} /></span><h3>Voice</h3></div>
          <p>{compatibleVoiceCount} compatible · {voices.length} catalog voices</p>
        </div>
        <VoiceLibrary
          voices={voices}
          languages={languages}
          selectedLanguages={form.supported_languages}
          selectedVoiceId={effectiveVoiceId}
          catalogState={catalog ? 'ready' : catalogError ? 'error' : 'loading'}
          catalogError={catalogError}
          notice={effectiveVoiceNotice}
          configurationLocked={!catalogUsable}
          preservedVoiceId={storedConfigurationPreserved ? form.voice_id : undefined}
          onSelect={(voiceId) => {
            setForm((current) => ({ ...current, voice_id: voiceId }));
            setVoiceNotice(null);
          }}
          onCatalogRefresh={onCatalogRefresh}
        />
      </section>

      <section className="editor-section editor-section-compact">
        <div className="editor-section-heading">
          <div><span className="section-icon"><Sparkles size={14} /></span><h3>Conversation tuning</h3></div>
        </div>
        <div className="form-grid">
          <div className="form-group">
            <label htmlFor={modelId}>Model</label>
            <select id={modelId} value={form.model_name} onChange={(event) => setForm({ ...form, model_name: event.target.value })}><option value="electron">Electron · voice optimized</option></select>
          </div>
          <div className="form-group">
            <label htmlFor={timezoneId}>Timezone</label>
            <input id={timezoneId} required list="common-timezones" value={form.timezone} onChange={(event) => setForm({ ...form, timezone: event.target.value })} />
            <datalist id="common-timezones"><option value="Asia/Dubai" /><option value="Asia/Kolkata" /><option value="Europe/London" /><option value="America/New_York" /><option value="America/Los_Angeles" /></datalist>
          </div>
          <div className="form-group range-control">
            <label htmlFor={speechRateId}>Speech rate <span>{form.speech_rate.toFixed(2)}×</span></label>
            <input id={speechRateId} type="range" min="0.5" max="2" step="0.05" value={form.speech_rate} onChange={(event) => setForm({ ...form, speech_rate: Number(event.target.value) })} />
          </div>
        </div>
      </section>

      <div className="editor-actions">
        <button type="button" className="btn btn-secondary" onClick={onCancel}>Cancel</button>
        <button
          type="submit"
          className="btn btn-primary"
          disabled={busy || !configurationGuard.allowed}
          title={!configurationGuard.allowed ? configurationGuard.reason ?? undefined : undefined}
        >
          {busy ? 'Saving…' : mode === 'create' ? 'Create local draft' : 'Save changes'}
        </button>
      </div>
    </form>
  );
}
