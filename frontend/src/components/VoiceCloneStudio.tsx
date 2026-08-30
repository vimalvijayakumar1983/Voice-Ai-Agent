import { FormEvent, useRef, useState } from 'react';
import { CheckCircle2, CircleAlert, LoaderCircle, Mic2, RefreshCw, ShieldCheck, Trash2, X } from 'lucide-react';
import { api, LanguageCatalogItem, VoiceClone } from '@/lib/api';
import styles from './VoiceCloneStudio.module.css';

interface VoiceCloneStudioProps {
  languages: LanguageCatalogItem[];
  selectedLanguages: string[];
  disabled?: boolean;
  onCatalogRefresh: () => Promise<void>;
  onSelect: (voiceId: string) => void;
}

const READY = new Set(['completed']);
const ACTIVE = new Set(['creating', 'pending', 'processing', 'creation_unknown']);

export default function VoiceCloneStudio({
  languages,
  selectedLanguages,
  disabled = false,
  onCatalogRefresh,
  onSelect,
}: VoiceCloneStudioProps) {
  const [open, setOpen] = useState(false);
  const [clones, setClones] = useState<VoiceClone[]>([]);
  const [loading, setLoading] = useState(false);
  const [working, setWorking] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ type: 'success' | 'error' | 'info'; text: string } | null>(null);
  const [displayName, setDisplayName] = useState('VAV Indian English');
  const [language, setLanguage] = useState(selectedLanguages[0] || 'en');
  const [accent, setAccent] = useState('indian');
  const [gender, setGender] = useState<'female' | 'male' | ''>('female');
  const [description, setDescription] = useState('Natural Indian English customer service voice');
  const [model, setModel] = useState<'lightning-v3.1' | 'lightning-v3.1-pro'>('lightning-v3.1-pro');
  const [consent, setConsent] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const loadClones = async () => {
    setLoading(true);
    try {
      setClones(await api.listVoiceClones());
    } catch (error) {
      setNotice({ type: 'error', text: message(error, 'Could not load custom voices.') });
    } finally {
      setLoading(false);
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!file) {
      setNotice({ type: 'error', text: 'Choose a clean 5–15 second voice sample.' });
      return;
    }
    setWorking('create');
    setNotice(null);
    try {
      const clone = await api.createVoiceClone({
        displayName,
        language,
        accent,
        gender,
        description,
        model,
        consentConfirmed: consent,
        file,
      });
      setClones((current) => [clone, ...current.filter((item) => item.id !== clone.id)]);
      setFile(null);
      setConsent(false);
      if (fileRef.current) fileRef.current.value = '';
      if (READY.has(clone.status) && clone.provider_voice_id) {
        await onCatalogRefresh();
        onSelect(clone.provider_voice_id);
        setNotice({ type: 'success', text: `${clone.display_name} is ready and selected for this agent.` });
      } else {
        setNotice({ type: 'info', text: `${clone.display_name} is processing. Use Check status before selecting it.` });
      }
    } catch (error) {
      setNotice({ type: 'error', text: message(error, 'Could not create the custom voice.') });
      await loadClones();
    } finally {
      setWorking(null);
    }
  };

  const refresh = async (clone: VoiceClone) => {
    setWorking(`refresh-${clone.id}`);
    setNotice(null);
    try {
      const updated = await api.refreshVoiceClone(clone.id);
      setClones((current) => current.map((item) => item.id === updated.id ? updated : item));
      if (READY.has(updated.status)) {
        await onCatalogRefresh();
        setNotice({ type: 'success', text: `${updated.display_name} is ready to preview and select.` });
      } else {
        setNotice({ type: 'info', text: `${updated.display_name} is ${statusLabel(updated.status).toLowerCase()}.` });
      }
    } catch (error) {
      setNotice({ type: 'error', text: message(error, 'Could not refresh the custom voice.') });
    } finally {
      setWorking(null);
    }
  };

  const remove = async (clone: VoiceClone) => {
    if (!window.confirm(`Delete ${clone.display_name}? This cannot be undone.`)) return;
    setWorking(`delete-${clone.id}`);
    setNotice(null);
    try {
      await api.deleteVoiceClone(clone.id);
      setClones((current) => current.filter((item) => item.id !== clone.id));
      await onCatalogRefresh();
      setNotice({ type: 'success', text: `${clone.display_name} was deleted.` });
    } catch (error) {
      setNotice({ type: 'error', text: message(error, 'Could not delete the custom voice.') });
    } finally {
      setWorking(null);
    }
  };

  if (!open) {
    return (
      <button type="button" className={styles.launch} disabled={disabled} onClick={() => { setOpen(true); void loadClones(); }}>
        <Mic2 size={16} aria-hidden="true" />
        <span><strong>Create a custom Indian voice</strong><small>Upload a consented 5–15 second recording</small></span>
      </button>
    );
  }

  return (
    <section className={styles.studio} aria-label="Custom voice studio">
      <header className={styles.header}>
        <div><span>Private voice studio</span><h4>Create a tenant-owned voice clone</h4><p>The recording is sent to Smallest.ai and is not retained by VAV.</p></div>
        <button type="button" className={styles.close} onClick={() => setOpen(false)} aria-label="Close custom voice studio"><X size={16} /></button>
      </header>

      {notice && (
        <div className={`${styles.notice} ${notice.type === 'error' ? styles.noticeError : notice.type === 'success' ? styles.noticeSuccess : ''}`} role={notice.type === 'error' ? 'alert' : 'status'}>
          {notice.type === 'error' ? <CircleAlert size={15} /> : <CheckCircle2 size={15} />}
          <span>{notice.text}</span>
        </div>
      )}

      <form className={styles.form} onSubmit={submit}>
        <label><span>Voice name</span><input required maxLength={255} value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></label>
        <label><span>Language</span><select value={language} onChange={(event) => setLanguage(event.target.value)}>{languages.map((item) => <option key={item.code} value={item.code}>{item.name}</option>)}</select></label>
        <label><span>Accent</span><input required maxLength={100} value={accent} onChange={(event) => setAccent(event.target.value)} /></label>
        <label><span>Voice profile</span><select value={gender} onChange={(event) => setGender(event.target.value as typeof gender)}><option value="female">Female</option><option value="male">Male</option><option value="">Not specified</option></select></label>
        <label className={styles.full}><span>Description</span><input maxLength={1000} value={description} onChange={(event) => setDescription(event.target.value)} /></label>
        <label><span>Quality</span><select value={model} onChange={(event) => setModel(event.target.value as typeof model)}><option value="lightning-v3.1-pro">Pro · recommended</option><option value="lightning-v3.1">Standard</option></select></label>
        <label><span>Voice sample</span><input ref={fileRef} required type="file" accept=".wav,.mp3,.webm,.mp4,audio/wav,audio/mpeg,audio/webm,audio/mp4,video/webm,video/mp4" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
        <p className={styles.guidance}>Use one speaker, no music or echo, 5–15 seconds, maximum 5 MB. Read naturally in the selected language.</p>
        <label className={`${styles.consent} ${styles.full}`}><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} /><ShieldCheck size={17} /><span>I confirm the speaker consented to creation and business use of this voice clone.</span></label>
        <div className={`${styles.actions} ${styles.full}`}><button type="submit" disabled={working === 'create' || !consent || !file}>{working === 'create' ? <LoaderCircle className="spin" size={15} /> : <Mic2 size={15} />} Create custom voice</button></div>
      </form>

      <div className={styles.inventory}>
        <div className={styles.inventoryHeading}><strong>Your custom voices</strong><button type="button" onClick={() => void loadClones()} disabled={loading}><RefreshCw className={loading ? 'spin' : ''} size={13} /> Refresh</button></div>
        {loading && clones.length === 0 ? <p>Loading custom voices…</p> : clones.length === 0 ? <p>No custom voices have been created in this workspace.</p> : clones.map((clone) => (
          <article key={clone.id} className={styles.clone}>
            <div><strong>{clone.display_name}</strong><small>{clone.language.toUpperCase()} · {clone.accent || 'Accent not specified'} · {clone.model.includes('pro') ? 'Pro' : 'Standard'}</small>{clone.last_error && <em>{clone.last_error}</em>}</div>
            <span className={READY.has(clone.status) ? styles.ready : ACTIVE.has(clone.status) ? styles.processing : styles.failed}>{statusLabel(clone.status)}</span>
            <div className={styles.cloneActions}>
              {READY.has(clone.status) && clone.provider_voice_id && <button type="button" onClick={async () => { await onCatalogRefresh(); onSelect(clone.provider_voice_id!); }}>Use voice</button>}
              {!READY.has(clone.status) && <button type="button" disabled={working === `refresh-${clone.id}`} onClick={() => void refresh(clone)}><RefreshCw className={working === `refresh-${clone.id}` ? 'spin' : ''} size={12} /> Check status</button>}
              <button type="button" className={styles.delete} disabled={working === `delete-${clone.id}`} onClick={() => void remove(clone)}><Trash2 size={12} /> Delete</button>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

function statusLabel(status: VoiceClone['status']) {
  const labels: Record<VoiceClone['status'], string> = {
    creating: 'Creating', pending: 'Pending', processing: 'Processing', completed: 'Ready',
    creation_unknown: 'Check required', error: 'Failed', missing: 'Missing',
    deletion_unknown: 'Deletion unknown', delete_error: 'Delete failed',
  };
  return labels[status];
}

function message(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback;
}
