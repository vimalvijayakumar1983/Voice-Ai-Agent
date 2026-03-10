'use client';

import { useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Phone, PhoneCall, Loader2, CheckCircle2, AlertCircle } from 'lucide-react';
import api from '@/lib/api';

const VOICE_OPTIONS = [
  { value: 'nova', label: 'Nova (Female, American)' },
  { value: 'alloy', label: 'Alloy (Female, Neutral)' },
  { value: 'echo', label: 'Echo (Male, American)' },
  { value: 'fable', label: 'Fable (Male, British)' },
  { value: 'onyx', label: 'Onyx (Male, Deep)' },
  { value: 'shimmer', label: 'Shimmer (Female, Warm)' },
  { value: 'rachel', label: 'Rachel (Female, Calm)' },
  { value: 'adam', label: 'Adam (Male, Deep)' },
  { value: 'antoni', label: 'Antoni (Male, Warm)' },
  { value: 'bella', label: 'Bella (Female, Soft)' },
  { value: 'domi', label: 'Domi (Female, Assertive)' },
  { value: 'elli', label: 'Elli (Female, Young)' },
  { value: 'josh', label: 'Josh (Male, Mature)' },
  { value: 'sam', label: 'Sam (Male, Energetic)' },
] as const;

const LANGUAGE_OPTIONS = [
  { value: 'en-US', label: 'English (US)' },
  { value: 'en-GB', label: 'English (UK)' },
  { value: 'ar', label: 'Arabic (عربي)' },
  { value: 'ar-AE', label: 'Arabic - UAE (عربي إماراتي)' },
  { value: 'ar-SA', label: 'Arabic - Saudi (عربي سعودي)' },
  { value: 'hi', label: 'Hindi (हिन्दी)' },
  { value: 'ur', label: 'Urdu (اردو)' },
  { value: 'ml', label: 'Malayalam (മലയാളം)' },
  { value: 'ta', label: 'Tamil (தமிழ்)' },
  { value: 'tl', label: 'Tagalog (Filipino)' },
  { value: 'fr', label: 'French (Français)' },
  { value: 'es', label: 'Spanish (Español)' },
  { value: 'de', label: 'German (Deutsch)' },
  { value: 'pt', label: 'Portuguese (Português)' },
  { value: 'zh', label: 'Chinese (中文)' },
  { value: 'ja', label: 'Japanese (日本語)' },
  { value: 'ko', label: 'Korean (한국어)' },
  { value: 'ru', label: 'Russian (Русский)' },
] as const;

interface DemoCallDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  agentId: string;
  agentName: string;
  agentVoice?: string;
  agentLanguage?: string;
}

type CallState = 'idle' | 'calling' | 'success' | 'error';

export function DemoCallDialog({
  open,
  onOpenChange,
  agentId,
  agentName,
  agentVoice,
  agentLanguage,
}: DemoCallDialogProps) {
  const [phoneNumber, setPhoneNumber] = useState('');
  const [fromNumber, setFromNumber] = useState('');
  const [selectedVoice, setSelectedVoice] = useState(agentVoice || 'nova');
  const [selectedLanguage, setSelectedLanguage] = useState(agentLanguage || 'en-US');
  const [callState, setCallState] = useState<CallState>('idle');
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState('');

  const handleDemoCall = async () => {
    if (!phoneNumber.trim()) return;

    setCallState('calling');
    setError('');
    setResult(null);

    try {
      const response = await api.post(`/api/v1/agents/${agentId}/demo-call`, {
        toNumber: phoneNumber.trim(),
        fromNumber: fromNumber.trim() || undefined,
        voice: selectedVoice,
        language: selectedLanguage,
      });
      setResult(response.data);
      setCallState('success');
    } catch (err: any) {
      const message = err.response?.data?.message || err.message || 'Failed to initiate demo call';
      setError(message);
      setCallState('error');
    }
  };

  const handleClose = () => {
    setCallState('idle');
    setResult(null);
    setError('');
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Phone className="h-5 w-5" />
            Demo Call
          </DialogTitle>
          <DialogDescription>
            Test <span className="font-medium text-foreground">{agentName}</span> by triggering a
            live call. The agent will call your number so you can evaluate its voice, speed, and
            conversational ability.
          </DialogDescription>
        </DialogHeader>

        {callState === 'idle' && (
          <div className="space-y-4">
            {/* Voice & Language selectors */}
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-2">
                <Label htmlFor="voice">Voice</Label>
                <Select value={selectedVoice} onValueChange={setSelectedVoice}>
                  <SelectTrigger id="voice">
                    <SelectValue placeholder="Select voice" />
                  </SelectTrigger>
                  <SelectContent>
                    {VOICE_OPTIONS.map((voice) => (
                      <SelectItem key={voice.value} value={voice.value}>
                        {voice.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <Label htmlFor="language">Language</Label>
                <Select value={selectedLanguage} onValueChange={setSelectedLanguage}>
                  <SelectTrigger id="language">
                    <SelectValue placeholder="Select language" />
                  </SelectTrigger>
                  <SelectContent>
                    {LANGUAGE_OPTIONS.map((lang) => (
                      <SelectItem key={lang.value} value={lang.value}>
                        {lang.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            {/* Phone number input */}
            <div className="space-y-2">
              <Label htmlFor="phone">Your phone number</Label>
              <Input
                id="phone"
                type="tel"
                placeholder="+971 50 123 4567"
                value={phoneNumber}
                onChange={(e) => setPhoneNumber(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Include country code (e.g. +971 for UAE, +1 for US, +91 for India)
              </p>
            </div>

            {/* Optional from number */}
            <div className="space-y-2">
              <Label htmlFor="from" className="flex items-center gap-1">
                From number <span className="text-muted-foreground">(optional)</span>
              </Label>
              <Input
                id="from"
                type="tel"
                placeholder="Uses default if empty"
                value={fromNumber}
                onChange={(e) => setFromNumber(e.target.value)}
              />
            </div>
          </div>
        )}

        {callState === 'calling' && (
          <div className="flex flex-col items-center gap-3 py-6">
            <Loader2 className="h-10 w-10 animate-spin text-primary" />
            <p className="text-sm text-muted-foreground">Initiating demo call...</p>
          </div>
        )}

        {callState === 'success' && result && (
          <div className="space-y-3">
            <div className="flex flex-col items-center gap-3 py-4">
              <CheckCircle2 className="h-10 w-10 text-green-500" />
              <p className="text-center text-sm font-medium">Demo call queued!</p>
            </div>
            <div className="rounded-md bg-green-50 p-3 text-sm dark:bg-green-950/30">
              <p>{result.message}</p>
            </div>
            <div className="space-y-1 text-sm text-muted-foreground">
              <p>Call ID: <code className="text-xs">{result.callId}</code></p>
              <p>To: {result.toNumber}</p>
              <p>From: {result.fromNumber}</p>
              <p>Voice: {VOICE_OPTIONS.find((v) => v.value === selectedVoice)?.label || selectedVoice}</p>
              <p>Language: {LANGUAGE_OPTIONS.find((l) => l.value === selectedLanguage)?.label || selectedLanguage}</p>
            </div>
          </div>
        )}

        {callState === 'error' && (
          <div className="space-y-3">
            <div className="flex flex-col items-center gap-3 py-4">
              <AlertCircle className="h-10 w-10 text-red-500" />
              <p className="text-center text-sm font-medium">Failed to initiate call</p>
            </div>
            <div className="rounded-md bg-red-50 p-3 text-sm text-red-700 dark:bg-red-950/30 dark:text-red-400">
              {error}
            </div>
          </div>
        )}

        <DialogFooter>
          {callState === 'idle' && (
            <>
              <Button variant="outline" onClick={handleClose}>
                Cancel
              </Button>
              <Button onClick={handleDemoCall} disabled={!phoneNumber.trim()}>
                <PhoneCall className="mr-2 h-4 w-4" />
                Start Demo Call
              </Button>
            </>
          )}
          {(callState === 'success' || callState === 'error') && (
            <>
              {callState === 'error' && (
                <Button
                  variant="outline"
                  onClick={() => setCallState('idle')}
                >
                  Try Again
                </Button>
              )}
              <Button onClick={handleClose}>
                Close
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
