# Direct Soniox speech in VAV

This change adds a selectable replacement for Inworld speech. It does not
automatically migrate existing agents, delete credentials, or change published
knowledge. No database migration is required.

## Runtime

Browser or SIP audio → LiveKit → Soniox STT v5 → direct OpenAI tool-calling
LLM → existing approved VAV knowledge / explicitly authorised MCP tools →
Soniox TTS v1 → LiveKit audio.

The initial direct LLM choices are GPT-4o mini and GPT-4o, matching VAV's
existing direct-OpenAI allowlist. Inworld Router's Luna model identifier is
not a direct-OpenAI model configuration and must not be copied across.

- Soniox key is validated and encrypted in Settings; never sent to the browser.
- The shared voice catalog and preview use Soniox directly.
- Calls use the supported LiveKit Soniox plugins, with local Silero VAD.
- Pinned transcription language is honoured; automatic recognition is restricted
  to the agent's declared languages. Final reported language selects TTS language.
- Existing tenant boundaries, knowledge releases, permission checks, call limits,
  recordings and retention controls remain in place.
- MCP checked-answer mode preserves tool calls but blocks unchecked LLM prose
  from reaching TTS. Approved answers still use the existing speech delivery path.
- Soniox STT/TTS costs are marked for reconciliation, not treated as free or priced
  using another vendor's rates. Direct OpenAI usage is reported separately.

## Deployment and activation

1. Merge after CI, then deploy backend, frontend AND LiveKit worker together.
2. Add the Soniox key in Settings. A separate OpenAI key is required.
3. Select Soniox and a catalog voice on a test agent using a real approved KB.
   A provider change resets phone readiness to draft. Check the direct LLM,
   language, speech speed, and retained MCP/recording policy before starting.
4. Run a browser call. Verify actual audio, transcript, a known factual answer,
   a paraphrase, unsupported information, interruption and a clean goodbye.
   Test English, Arabic and Hindi separately before enabling language switching.
5. Test private ERP only from an authorised staff browser agent with its existing
   company-scoped MCP grants. Verify exact figures and conversational summaries.
6. Run readiness before phone activation: catalog, short TTS synthesis, STT
   streaming completion and LLM tool-calling are checked. A successful probe is
   not a substitute for real audio, carrier and accuracy tests.

Provider credentials and a real call are required to validate live quality,
latency, pronunciation and billing. Mock tests cannot establish those outcomes.
If a canary fails, retain the existing production agent while investigating;
there is no automatic cross-provider fallback in this change.

## Knowledge scope

Soniox is eligible for the existing VAV knowledge binding and serving lifecycle.
No extraction, search-ranking, re-index or doctor-directory repair is included.
Speech migration by itself cannot repair missing or incorrectly retrieved facts.
