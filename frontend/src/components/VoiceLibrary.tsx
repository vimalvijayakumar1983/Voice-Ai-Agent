import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  Check,
  ChevronDown,
  ChevronUp,
  CircleHelp,
  Filter,
  Info,
  LoaderCircle,
  LockKeyhole,
  Pause,
  Play,
  Search,
  Volume2,
} from 'lucide-react';
import { api, LanguageCatalogItem, VoiceCatalogItem } from '@/lib/api';
import {
  filterAndSortVoices,
  languageMatches,
  voiceCompatibility,
  voicePreviewAvailability,
  voiceTier,
} from './voice-library.cjs';
import type { VoiceCompatibility, VoiceCompatibilityStatus } from './voice-library.cjs';
import VoiceCloneStudio from './VoiceCloneStudio';
import styles from './VoiceLibrary.module.css';

type CatalogState = 'loading' | 'ready' | 'error';
type VoiceStatusFilter = VoiceCompatibilityStatus | 'all';
type PreviewStatus = 'idle' | 'loading' | 'ready' | 'playing' | 'paused' | 'error';

interface PreviewState {
  voiceId: string | null;
  voiceName: string | null;
  status: PreviewStatus;
  error: string | null;
}

const idlePreview: PreviewState = {
  voiceId: null,
  voiceName: null,
  status: 'idle',
  error: null,
};

interface VoiceLibraryProps {
  voices: VoiceCatalogItem[];
  languages: LanguageCatalogItem[];
  selectedLanguages: string[];
  selectedVoiceId: string;
  catalogState: CatalogState;
  catalogError?: string | null;
  notice?: string | null;
  configurationLocked?: boolean;
  preservedVoiceId?: string;
  onSelect: (voiceId: string) => void;
  onCatalogRefresh: () => Promise<void>;
}

const PAGE_SIZE = 18;

export default function VoiceLibrary({
  voices,
  languages,
  selectedLanguages,
  selectedVoiceId,
  catalogState,
  catalogError,
  notice,
  configurationLocked = false,
  preservedVoiceId,
  onSelect,
  onCatalogRefresh,
}: VoiceLibraryProps) {
  const searchId = useId();
  const tierId = useId();
  const genderId = useId();
  const accentId = useId();
  const resultsId = useId();
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<VoiceStatusFilter>('compatible');
  const [tierFilter, setTierFilter] = useState('all');
  const [genderFilter, setGenderFilter] = useState('all');
  const [accentFilter, setAccentFilter] = useState('all');
  const [resultLimit, setResultLimit] = useState(PAGE_SIZE);
  const [preview, setPreview] = useState<PreviewState>(idlePreview);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const objectUrlRef = useRef<string | null>(null);
  const previewOperationRef = useRef(0);

  const releaseAudio = useCallback(() => {
    const audio = audioRef.current;
    if (audio) {
      audio.onplay = null;
      audio.onpause = null;
      audio.onended = null;
      audio.onerror = null;
      audio.pause();
      audio.removeAttribute('src');
      audioRef.current = null;
    }
    if (objectUrlRef.current) {
      URL.revokeObjectURL(objectUrlRef.current);
      objectUrlRef.current = null;
    }
  }, []);

  const stopPreview = useCallback(() => {
    previewOperationRef.current += 1;
    releaseAudio();
    setPreview(idlePreview);
  }, [releaseAudio]);

  useEffect(() => () => {
    previewOperationRef.current += 1;
    releaseAudio();
  }, [releaseAudio]);

  const languageNames = useMemo(
    () => new Map(languages.map((language) => [language.code, language.name])),
    [languages],
  );
  const selectedVoice = voices.find((voice) => voice.id === selectedVoiceId);
  const providerName = voices[0]?.provider === 'sarvam' ? 'Sarvam AI' : 'Smallest.ai';
  const selectedCompatibility = selectedVoice
    ? voiceCompatibility(selectedVoice, selectedLanguages)
    : null;

  const counts = useMemo(() => {
    const result = { compatible: 0, incompatible: 0, unknown: 0, unavailable: 0 };
    for (const voice of voices) {
      result[voiceCompatibility(voice, selectedLanguages).status] += 1;
    }
    return result;
  }, [selectedLanguages, voices]);

  const accents = useMemo(
    () => uniqueSorted(voices.map((voice) => voice.accent)),
    [voices],
  );
  const genders = useMemo(
    () => uniqueSorted(voices.map((voice) => voice.gender?.toLowerCase() ?? null)),
    [voices],
  );
  const tiers = useMemo(
    () => uniqueSorted(voices.map((voice) => voiceTier(voice).toLowerCase())),
    [voices],
  );

  const filteredVoices = useMemo(
    () => filterAndSortVoices(voices, {
      selectedLanguages,
      query,
      status: statusFilter,
      tier: tierFilter as 'standard' | 'pro' | 'cloned' | 'provider-routed' | 'unverified' | 'all',
      gender: genderFilter,
      accent: accentFilter,
    }),
    [accentFilter, genderFilter, query, selectedLanguages, statusFilter, tierFilter, voices],
  );
  const visibleVoices = filteredVoices.slice(0, resultLimit);

  const resetPagination = () => setResultLimit(PAGE_SIZE);
  const updateStatus = (status: VoiceStatusFilter) => {
    setStatusFilter(status);
    resetPagination();
  };

  const playPreview = async (voice: VoiceCatalogItem) => {
    const previewAvailability = voicePreviewAvailability(voice);
    if (!previewAvailability.available) {
      setPreview({
        voiceId: voice.id,
        voiceName: voice.name,
        status: 'error',
        error: previewAvailability.reason,
      });
      return;
    }

    const existingAudio = audioRef.current;
    if (preview.voiceId === voice.id && existingAudio && preview.status !== 'error') {
      if (preview.status === 'playing') {
        existingAudio.pause();
        return;
      }
      try {
        await existingAudio.play();
      } catch (error) {
        setPreview({
          voiceId: voice.id,
          voiceName: voice.name,
          status: 'error',
          error: previewErrorMessage(error),
        });
      }
      return;
    }

    previewOperationRef.current += 1;
    const operation = previewOperationRef.current;
    releaseAudio();
    setPreview({ voiceId: voice.id, voiceName: voice.name, status: 'loading', error: null });

    try {
      const blob = await api.previewVoice(voice.provider, voice.id, selectedLanguages[0]);
      if (previewOperationRef.current !== operation) return;

      const objectUrl = URL.createObjectURL(blob);
      const audio = new Audio(objectUrl);
      objectUrlRef.current = objectUrl;
      audioRef.current = audio;
      audio.preload = 'metadata';
      audio.onplay = () => {
        if (previewOperationRef.current === operation) {
          setPreview({ voiceId: voice.id, voiceName: voice.name, status: 'playing', error: null });
        }
      };
      audio.onpause = () => {
        if (previewOperationRef.current === operation && !audio.ended) {
          setPreview({ voiceId: voice.id, voiceName: voice.name, status: 'paused', error: null });
        }
      };
      audio.onended = () => {
        if (previewOperationRef.current === operation) {
          audio.currentTime = 0;
          setPreview({ voiceId: voice.id, voiceName: voice.name, status: 'ready', error: null });
        }
      };
      audio.onerror = () => {
        if (previewOperationRef.current === operation) {
          setPreview({
            voiceId: voice.id,
            voiceName: voice.name,
            status: 'error',
            error: 'The preview audio could not be played. Try again.',
          });
        }
      };

      setPreview({ voiceId: voice.id, voiceName: voice.name, status: 'ready', error: null });
      await audio.play();
    } catch (error) {
      if (previewOperationRef.current !== operation) return;
      releaseAudio();
      setPreview({
        voiceId: voice.id,
        voiceName: voice.name,
        status: 'error',
        error: previewErrorMessage(error),
      });
    }
  };

  const closeLibrary = () => {
    stopPreview();
    setLibraryOpen(false);
  };

  return (
    <div className={styles.root}>
      {notice && (
        <div className={styles.notice} role="status" aria-live="polite">
          <AlertCircle size={16} aria-hidden="true" />
          <span>{notice}</span>
        </div>
      )}

      {configurationLocked && (
        <div className={styles.configurationLock} role="status">
          <LockKeyhole size={16} aria-hidden="true" />
          <span>{preservedVoiceId
            ? `Stored voice ${preservedVoiceId} is preserved. Voice and language controls unlock when the catalog is available.`
            : 'Voice selection is locked until the selected provider catalog is available.'}</span>
        </div>
      )}

      <SelectedVoiceSummary
        voice={selectedVoice}
        compatibility={selectedCompatibility}
        selectedLanguages={selectedLanguages}
        languageNames={languageNames}
        libraryOpen={libraryOpen}
        configurationLocked={configurationLocked}
        preservedVoiceId={preservedVoiceId}
        preview={preview}
        onPreview={selectedVoice ? () => playPreview(selectedVoice) : undefined}
        onBrowse={() => {
          if (libraryOpen) closeLibrary();
          else setLibraryOpen(true);
        }}
        onUseDefault={selectedLanguages.length === 1 && selectedVoiceId ? () => {
          stopPreview();
          onSelect('');
        } : undefined}
      />

      <LanguageReadiness
        selectedLanguages={selectedLanguages}
        languageNames={languageNames}
        voice={selectedVoice}
        compatibility={selectedCompatibility}
        configurationLocked={configurationLocked}
        preservedVoiceId={preservedVoiceId}
      />
      {voices[0]?.provider !== 'sarvam' ? (
        <VoiceCloneStudio
          languages={languages}
          selectedLanguages={selectedLanguages}
          disabled={configurationLocked}
          onCatalogRefresh={onCatalogRefresh}
          onSelect={onSelect}
        />
      ) : null}
      <PreviewAnnouncement preview={preview} />
      {libraryOpen && (
        <section className={styles.library} aria-labelledby={`${resultsId}-heading`}>
          <div className={styles.libraryHeading}>
            <div>
              <span className={styles.eyebrow}>{providerName} voice catalog</span>
              <h4 id={`${resultsId}-heading`}>Choose a voice with coverage for every language</h4>
              <p>Compatibility uses the full set of published voice-language and Atoms model metadata. It does not prove same-call detection; test the exact combination before activation.</p>
            </div>
            <button type="button" className={styles.closeButton} onClick={closeLibrary}>
              Close <ChevronUp size={15} aria-hidden="true" />
            </button>
          </div>

          <div className={styles.statusTabs} aria-label="Voice availability" role="group">
            <StatusFilterButton
              active={statusFilter === 'compatible'}
              count={counts.compatible}
              label="Compatible"
              onClick={() => updateStatus('compatible')}
            />
            <StatusFilterButton
              active={statusFilter === 'all'}
              count={voices.length}
              label="All voices"
              onClick={() => updateStatus('all')}
            />
            <StatusFilterButton
              active={statusFilter === 'unavailable'}
              count={counts.unavailable}
              label="Unavailable"
              onClick={() => updateStatus('unavailable')}
            />
          </div>

          <div className={styles.toolbar}>
            <label className={styles.search} htmlFor={searchId}>
              <Search size={17} aria-hidden="true" />
              <span className={styles.srOnly}>Search voices</span>
              <input
                id={searchId}
                value={query}
                type="search"
                placeholder="Search name, language, accent, style or ID"
                onChange={(event) => {
                  setQuery(event.target.value);
                  resetPagination();
                }}
                aria-controls={resultsId}
              />
            </label>
            <div className={styles.filterIcon} aria-hidden="true"><Filter size={16} /></div>
            <FilterSelect
              id={tierId}
              label="Tier"
              value={tierFilter}
              values={tiers}
              onChange={(value) => { setTierFilter(value); resetPagination(); }}
            />
            <FilterSelect
              id={genderId}
              label="Gender"
              value={genderFilter}
              values={genders}
              onChange={(value) => { setGenderFilter(value); resetPagination(); }}
            />
            <FilterSelect
              id={accentId}
              label="Accent"
              value={accentFilter}
              values={accents}
              onChange={(value) => { setAccentFilter(value); resetPagination(); }}
            />
          </div>

          <CatalogStatus
            state={catalogState}
            error={catalogError}
            voiceCount={voices.length}
            filteredCount={filteredVoices.length}
            resultId={resultsId}
          >
            <div className={styles.resultsHeader}>
              <p aria-live="polite">
                <strong>{filteredVoices.length}</strong> {filteredVoices.length === 1 ? 'voice' : 'voices'}
                {statusFilter === 'compatible' ? ' matched to this language set' : ' shown'}
              </p>
              {(counts.incompatible > 0 || counts.unknown > 0) && (
                <p className={styles.resultsContext}>
                  {counts.incompatible} incompatible · {counts.unknown} unverified
                </p>
              )}
            </div>
            <div className={styles.voiceGrid} id={resultsId} aria-label="Voice results">
              {visibleVoices.map(({ voice, compatibility }) => (
                <VoiceCard
                  key={voice.id}
                  voice={voice}
                  compatibility={compatibility}
                  selected={voice.id === selectedVoiceId}
                  selectedLanguages={selectedLanguages}
                  languageNames={languageNames}
                  preview={preview}
                  onPreview={() => playPreview(voice)}
                  onSelect={() => {
                    stopPreview();
                    onSelect(voice.id);
                  }}
                />
              ))}
            </div>
            {visibleVoices.length < filteredVoices.length && (
              <button
                type="button"
                className={styles.loadMore}
                onClick={() => setResultLimit((limit) => limit + PAGE_SIZE)}
              >
                Show {Math.min(PAGE_SIZE, filteredVoices.length - visibleVoices.length)} more voices
              </button>
            )}
          </CatalogStatus>
        </section>
      )}
    </div>
  );
}

function SelectedVoiceSummary({
  voice,
  compatibility,
  selectedLanguages,
  languageNames,
  libraryOpen,
  configurationLocked,
  preservedVoiceId,
  preview,
  onPreview,
  onBrowse,
  onUseDefault,
}: {
  voice: VoiceCatalogItem | undefined;
  compatibility: VoiceCompatibility | null;
  selectedLanguages: string[];
  languageNames: Map<string, string>;
  libraryOpen: boolean;
  configurationLocked: boolean;
  preservedVoiceId?: string;
  preview: PreviewState;
  onPreview?: () => void;
  onBrowse: () => void;
  onUseDefault?: () => void;
}) {
  const safeSelection = voice && compatibility?.status === 'compatible';
  const voicePreview = preview.voiceId === voice?.id ? preview : idlePreview;
  const previewAvailability = voicePreviewAvailability(voice);
  const title = voice?.name
    ?? (preservedVoiceId ? `Stored voice · ${preservedVoiceId}` : configurationLocked ? 'Voice catalog required' : 'No verified voice selected');
  return (
    <div className={`${styles.selection} ${safeSelection ? styles.selectionReady : styles.selectionNeedsAttention}`}>
      <div className={styles.voiceAvatar}><Volume2 size={20} aria-hidden="true" /></div>
      <div className={styles.selectionCopy}>
        <div className={styles.selectionTitle}>
          <strong>{title}</strong>
          {voice && compatibility && <StatusBadge status={compatibility.status} />}
        </div>
        {voice ? (
          <p>
            {voiceTier(voice)} · {modelLabel(voice.synthesizer_model)}
            {voice.accent ? ` · ${voice.accent}` : ''}
          </p>
        ) : preservedVoiceId ? (
          <p>{configurationLocked
            ? 'The catalog is unavailable. This stored selection will remain unchanged when you save unrelated agent fields.'
            : 'This stored selection is not verifiable in the current catalog. Replace it before changing voice or language configuration.'}</p>
        ) : (
          <p>
            {selectedLanguages.length > 1
              ? `Choose one voice whose catalog metadata covers ${languageList(selectedLanguages, languageNames)}.`
              : 'The provider default can be used for a draft, but its language coverage is not verified.'}
          </p>
        )}
      </div>
      <div className={styles.selectionActions}>
        <button
          type="button"
          className={`${styles.previewButton} ${voicePreview.status === 'playing' ? styles.previewPlaying : ''}`}
          disabled={!previewAvailability.available || voicePreview.status === 'loading'}
          title={!previewAvailability.available ? previewAvailability.reason ?? undefined : undefined}
          onClick={onPreview}
        >
          <PreviewButtonIcon preview={voicePreview} /> {previewButtonLabel(voicePreview, previewAvailability.available)}
        </button>
        {onUseDefault && (
          <button type="button" className={styles.textButton} onClick={onUseDefault}>Use provider default</button>
        )}
        <button
          type="button"
          className={styles.browseButton}
          onClick={onBrowse}
          aria-expanded={libraryOpen}
          disabled={configurationLocked}
        >
          {configurationLocked ? 'Catalog unavailable' : voice || preservedVoiceId ? 'Change voice' : 'Browse voices'}
          {libraryOpen ? <ChevronUp size={15} aria-hidden="true" /> : <ChevronDown size={15} aria-hidden="true" />}
        </button>
      </div>
    </div>
  );
}

function LanguageReadiness({
  selectedLanguages,
  languageNames,
  voice,
  compatibility,
  configurationLocked,
  preservedVoiceId,
}: {
  selectedLanguages: string[];
  languageNames: Map<string, string>;
  voice: VoiceCatalogItem | undefined;
  compatibility: VoiceCompatibility | null;
  configurationLocked: boolean;
  preservedVoiceId?: string;
}) {
  return (
    <div className={styles.readiness}>
      <div className={styles.readinessHeading}>
        <div>
          <strong>Language readiness</strong>
          <span>{configurationLocked && preservedVoiceId
            ? 'Coverage revalidation is paused; the stored configuration remains unchanged.'
            : 'Every language needs catalog coverage before this voice can be published.'}</span>
        </div>
        <span className={styles.readinessCount}>
          {configurationLocked && preservedVoiceId
            ? 'Stored configuration'
            : `${voice && compatibility?.status === 'compatible' ? selectedLanguages.length : 0}/${selectedLanguages.length} ready`}
        </span>
      </div>
      <div className={styles.languageChecks}>
        {selectedLanguages.map((language) => {
          const covered = Boolean(
            voice
            && compatibility?.status === 'compatible'
            && voice.languages.some((capability) => languageMatches(capability, language)),
          );
          return (
            <span className={covered ? styles.languageReady : styles.languagePending} key={language}>
              {covered
                ? <Check size={13} aria-hidden="true" />
                : configurationLocked && preservedVoiceId
                  ? <LockKeyhole size={13} aria-hidden="true" />
                  : <CircleHelp size={13} aria-hidden="true" />}
              {languageNames.get(language) ?? language.toUpperCase()}
            </span>
          );
        })}
      </div>
    </div>
  );
}

function VoiceCard({
  voice,
  compatibility,
  selected,
  selectedLanguages,
  languageNames,
  preview,
  onPreview,
  onSelect,
}: {
  voice: VoiceCatalogItem;
  compatibility: VoiceCompatibility;
  selected: boolean;
  selectedLanguages: string[];
  languageNames: Map<string, string>;
  preview: PreviewState;
  onPreview: () => void;
  onSelect: () => void;
}) {
  const selectable = compatibility.status === 'compatible';
  const voicePreview = preview.voiceId === voice.id ? preview : idlePreview;
  const previewAvailability = voicePreviewAvailability(voice);
  const missing = compatibility.missingLanguages
    .map((language) => languageNames.get(language) ?? language.toUpperCase())
    .join(', ');
  return (
    <article className={`${styles.voiceCard} ${selected ? styles.voiceCardSelected : ''} ${!selectable ? styles.voiceCardDisabled : ''}`}>
      <button
        type="button"
        className={styles.voiceChoice}
        onClick={selectable ? onSelect : undefined}
        aria-disabled={!selectable}
        aria-pressed={selected}
        aria-label={`${selectable ? 'Select' : 'Unavailable'} ${voice.name}`}
      >
        <span className={styles.voiceChoiceTop}>
          <span className={styles.voiceMiniAvatar}><Volume2 size={16} aria-hidden="true" /></span>
          <span className={styles.voiceIdentity}>
            <strong>{voice.name}</strong>
            <small>{voice.accent || 'Accent not specified'}{voice.gender ? ` · ${titleCase(voice.gender)}` : ''}</small>
          </span>
          <StatusBadge status={compatibility.status} />
        </span>
        <span className={styles.voiceMeta}>
          <span>{voiceTier(voice)}</span>
          <span>{modelLabel(voice.synthesizer_model)}</span>
          {voice.source === 'cloned' && <span>Your workspace</span>}
        </span>
        <span className={styles.coverage}>
          {selectable
            ? `Covers ${languageList(selectedLanguages, languageNames)}`
            : compatibility.status === 'incompatible'
              ? `Missing ${missing}`
              : compatibility.reason}
        </span>
        {voice.use_cases.length > 0 && (
          <span className={styles.useCases}>{voice.use_cases.slice(0, 3).join(' · ')}</span>
        )}
        <span className={styles.voiceId}>{voice.id}</span>
      </button>
      <button
        type="button"
        className={`${styles.cardPreview} ${voicePreview.status === 'playing' ? styles.previewPlaying : ''}`}
        disabled={!previewAvailability.available || voicePreview.status === 'loading'}
        title={!previewAvailability.available ? previewAvailability.reason ?? undefined : undefined}
        aria-label={`${previewButtonLabel(voicePreview, previewAvailability.available)} for ${voice.name}`}
        onClick={onPreview}
      >
        <PreviewButtonIcon preview={voicePreview} compact /> {previewButtonLabel(voicePreview, previewAvailability.available)}
      </button>
    </article>
  );
}

function StatusBadge({ status }: { status: VoiceCompatibilityStatus }) {
  const labels: Record<VoiceCompatibilityStatus, string> = {
    compatible: 'Compatible',
    incompatible: 'Missing language',
    unknown: 'Unverified',
    unavailable: 'Unavailable',
  };
  return <span className={`${styles.statusBadge} ${styles[`status${titleCase(status)}`]}`}>{labels[status]}</span>;
}

function StatusFilterButton({
  active,
  count,
  label,
  onClick,
}: {
  active: boolean;
  count: number;
  label: string;
  onClick: () => void;
}) {
  return (
    <button type="button" aria-pressed={active} className={active ? styles.statusTabActive : styles.statusTab} onClick={onClick}>
      {label}<span>{count}</span>
    </button>
  );
}

function FilterSelect({
  id,
  label,
  value,
  values,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  values: string[];
  onChange: (value: string) => void;
}) {
  return (
    <label className={styles.filterSelect} htmlFor={id}>
      <span>{label}</span>
      <select id={id} value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="all">All</option>
        {values.map((item) => <option value={item} key={item}>{titleCase(item)}</option>)}
      </select>
    </label>
  );
}

function CatalogStatus({
  state,
  error,
  voiceCount,
  filteredCount,
  resultId,
  children,
}: {
  state: CatalogState;
  error?: string | null;
  voiceCount: number;
  filteredCount: number;
  resultId: string;
  children: React.ReactNode;
}) {
  if (state === 'loading') {
    return <div className={styles.catalogState} id={resultId} role="status"><LoaderCircle className={styles.loadingIcon} size={20} aria-hidden="true" />Connecting to the Smallest.ai voice catalog…</div>;
  }
  if (state === 'error') {
    return <div className={styles.catalogState} id={resultId} role="alert"><AlertCircle size={20} />{error || 'The voice catalog could not be loaded. Try again before choosing a voice.'}</div>;
  }
  if (voiceCount === 0) {
    return <div className={styles.catalogState} id={resultId}><Info size={20} />No selectable voices were returned by Smallest.ai.</div>;
  }
  if (filteredCount === 0) {
    return <div className={styles.catalogState} id={resultId}><Search size={20} />No voices match these languages and filters. Adjust the filters or language set.</div>;
  }
  return <>{children}</>;
}

function PreviewAnnouncement({ preview }: { preview: PreviewState }) {
  if (preview.status === 'idle') return null;
  const message = preview.status === 'error'
    ? `${preview.voiceName ?? 'Voice'} preview failed. ${preview.error}`
    : preview.status === 'loading'
      ? `Loading ${preview.voiceName} preview.`
      : preview.status === 'playing'
        ? `Playing ${preview.voiceName} preview.`
        : preview.status === 'paused'
          ? `${preview.voiceName} preview paused.`
          : `${preview.voiceName} preview ready.`;
  return (
    <div
      className={`${styles.previewAnnouncement} ${preview.status === 'error' ? styles.previewAnnouncementError : ''}`}
      role={preview.status === 'error' ? 'alert' : 'status'}
      aria-live={preview.status === 'error' ? 'assertive' : 'polite'}
    >
      {preview.status === 'error' ? <AlertCircle size={15} aria-hidden="true" /> : <Volume2 size={15} aria-hidden="true" />}
      <span>{message}</span>
    </div>
  );
}

function PreviewButtonIcon({ preview, compact = false }: { preview: PreviewState; compact?: boolean }) {
  const size = compact ? 13 : 14;
  if (preview.status === 'loading') return <LoaderCircle className={styles.loadingIcon} size={size} aria-hidden="true" />;
  if (preview.status === 'playing') return <Pause size={size} aria-hidden="true" />;
  return <Play size={size} aria-hidden="true" />;
}

function uniqueSorted(values: Array<string | null | undefined>) {
  return Array.from(new Set(values.filter((value): value is string => Boolean(value)))).sort((a, b) => a.localeCompare(b));
}

function languageList(codes: string[], languageNames: Map<string, string>) {
  return codes.map((code) => languageNames.get(code) ?? code.toUpperCase()).join(' + ');
}

function modelLabel(model: string | null) {
  if (!model) return 'Model unverified';
  return model
    .replace(/^waves_/, '')
    .replace(/v(\d+)_(\d+)/i, 'v$1.$2')
    .replaceAll('_', ' ')
    .replace(/\bv(\d)/i, 'v$1')
    .replace(/\blightning\b/i, 'Lightning');
}

function previewButtonLabel(preview: PreviewState, available: boolean) {
  if (!available) return 'Preview unavailable';
  if (preview.status === 'loading') return 'Loading preview…';
  if (preview.status === 'playing') return 'Pause preview';
  if (preview.status === 'paused') return 'Resume preview';
  if (preview.status === 'error') return 'Retry preview';
  if (preview.status === 'ready') return 'Replay preview';
  return 'Preview voice';
}

function previewErrorMessage(error: unknown) {
  return error instanceof Error ? error.message : 'The preview could not be loaded. Try again.';
}

function titleCase(value: string) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
