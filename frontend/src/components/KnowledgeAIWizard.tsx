import { FormEvent, useId, useState } from 'react';
import { BookOpenCheck, Loader2, ShieldCheck, Sparkles } from 'lucide-react';
import { KnowledgeAIDraftRequest } from '@/lib/api';
import styles from '@/styles/Knowledge.module.css';

interface KnowledgeAIWizardProps {
  busy: boolean;
  onCancel: () => void;
  onGenerate: (request: KnowledgeAIDraftRequest) => Promise<void>;
}

const scopes: Array<{ value: KnowledgeAIDraftRequest['scope_preference']; label: string }> = [
  { value: 'auto', label: 'Let AI recommend the scope' },
  { value: 'workspace', label: 'Whole workspace' },
  { value: 'group', label: 'Group' },
  { value: 'division', label: 'Division' },
  { value: 'branch', label: 'Branch' },
  { value: 'department', label: 'Department' },
];

export default function KnowledgeAIWizard({ busy, onCancel, onGenerate }: KnowledgeAIWizardProps) {
  const [brief, setBrief] = useState('');
  const [scope, setScope] = useState<KnowledgeAIDraftRequest['scope_preference']>('auto');
  const [language, setLanguage] = useState('en');
  const briefId = useId();
  const scopeId = useId();
  const languageId = useId();

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    await onGenerate({
      brief: brief.trim(),
      scope_preference: scope,
      primary_language: language.trim().toLowerCase(),
    });
  };

  return <section className={styles.createPanel} aria-labelledby="knowledge-ai-heading">
    <div className={styles.createAside}>
      <Sparkles size={21} />
      <span className="page-kicker">OpenAI knowledge architect</span>
      <h2 id="knowledge-ai-heading">Design governed knowledge metadata</h2>
      <p>Describe the business context. OpenAI drafts the scope and source plan; VAV validates it before anything can be saved.</p>
      <div>
        <span><ShieldCheck size={14} /> Review before save</span>
        <span><BookOpenCheck size={14} /> No invented source content</span>
        <span><Sparkles size={14} /> Zero automatic indexing</span>
      </div>
    </div>
    <form className={styles.createForm} onSubmit={submit}>
      <div className="form-group">
        <label htmlFor={briefId}>Business and knowledge brief <span>{brief.length}/4000</span></label>
        <textarea
          id={briefId}
          required
          minLength={20}
          maxLength={4000}
          rows={8}
          value={brief}
          placeholder="Example: Create knowledge for Adam & Eve Cosmetic Medical Centre covering approved treatments, doctors, locations, opening hours, appointment policies, and customer FAQs. Do not include unverified prices or medical advice."
          onChange={(event) => setBrief(event.target.value)}
        />
        <p className="form-hint">Include the business, intended callers, approved topics, excluded topics, and the kinds of sources you will provide. This description is sent to your configured OpenAI workspace.</p>
      </div>
      <div className="form-grid">
        <div className="form-group">
          <label htmlFor={scopeId}>Business scope</label>
          <select id={scopeId} value={scope} onChange={(event) => setScope(event.target.value as KnowledgeAIDraftRequest['scope_preference'])}>
            {scopes.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
        </div>
        <div className="form-group">
          <label htmlFor={languageId}>Primary content language</label>
          <input id={languageId} required maxLength={20} value={language} placeholder="en" onChange={(event) => setLanguage(event.target.value)} />
          <p className="form-hint">Use a language code such as en, ar, hi, ml, or en-GB.</p>
        </div>
      </div>
      <div className={styles.formActions}>
        <button type="button" className="btn btn-ghost" disabled={busy} onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn-primary" disabled={busy || brief.trim().length < 20 || language.trim().length < 2}>
          {busy ? <><Loader2 className="spin" size={14} /> Generating draft…</> : <><Sparkles size={14} /> Generate reviewable draft</>}
        </button>
      </div>
    </form>
  </section>;
}
