import { useEffect, useState } from 'react';
import { api } from '@/lib/api';

// Explicit consent is per call, never saved as a browser-wide default.
export default function RecordingControls({ callId, connected }: { callId: string; connected: boolean }) {
  const [config, setConfig] = useState<{ enabled: boolean; notice: string; notice_version: string } | null>(null);
  const [consent, setConsent] = useState(false);
  const [status, setStatus] = useState('off');
  const [available, setAvailable] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [audioUrl, setAudioUrl] = useState('');

  useEffect(() => { let live = true; api.recordingConfiguration().then(value => { if (live) setConfig(value); }).catch(() => {}); return () => { live = false; }; }, []);
  useEffect(() => {
    let live = true;
    let terminal = false;
    const refresh = () => {
      if (terminal) return;
      api.recordingStatus(callId).then(value => {
        if (live) {
          setStatus(value.state); setAvailable(value.available);
          terminal = ['ready', 'failed', 'expired'].includes(value.state);
        }
      }).catch(() => {});
    };
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => { live = false; clearInterval(timer); };
  }, [callId]);
  useEffect(() => () => { if (audioUrl) URL.revokeObjectURL(audioUrl); }, [audioUrl]);

  async function action(kind: 'start' | 'stop' | 'play') {
    setBusy(true); setError('');
    try {
      if (kind === 'play') {
        const blob = await api.getCallRecording(callId);
        setAudioUrl(URL.createObjectURL(blob));
      } else {
        if (!config) throw new Error('Recording settings are unavailable. Refresh and try again.');
        const value = kind === 'start'
          ? await api.startRecording(callId, config.notice_version)
          : await api.stopRecording(callId);
        setStatus(value.state); setAvailable(value.available);
      }
    } catch (e) { setError(e instanceof Error ? e.message : 'Recording operation could not be confirmed.'); }
    finally { setBusy(false); }
  }

  if (!config?.enabled && status === 'off') return null;
  return <section aria-label="Private call recording" style={{ padding: 16, border: '1px solid #777', borderRadius: 8, marginTop: 12 }}>
    <strong>Private recording: {status}</strong>
    {['off', 'retryable'].includes(status) && connected && <>
      <p>{config?.notice}</p>
      <label><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} /> I am the caller and agree to this recording.</label>
      <button type="button" disabled={!consent || busy || !config?.enabled} onClick={() => void action('start')}>Start recording</button>
      <p>Recording begins only after you press Start recording. Earlier audio is not included.</p>
    </>}
    {connected && ['recording', 'preparing'].includes(status) && <button type="button" disabled={busy} onClick={() => void action('stop')}>Stop recording</button>}
    {status === 'unconfirmed' && <p role="alert">Recording could not be confirmed. End this call to stop any capture.</p>}
    {status === 'processing' && <p>Finalizing recording. Ending the call also stops capture.</p>}
    {available && <button type="button" disabled={busy} onClick={() => void action('play')}>Load private recording</button>}
    {audioUrl && <audio controls src={audioUrl} />}
    {error && <p role="alert">{error}</p>}
    <p>Access expires after 90 days. Storage is outside the UAE. Only authorized workspace users can access recordings.</p>
  </section>;
}
