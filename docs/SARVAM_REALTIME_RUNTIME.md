# Sarvam realtime runtime runbook

VAV can serve Sarvam agents without creating a hosted Smallest.ai agent. The
serving path is:

1. Twilio sends bidirectional 8 kHz μ-law audio to the VAV WebSocket.
2. Sarvam Saaras v3 performs realtime transcription and VAD.
3. VAV retrieves approved text from the agent's bound knowledge base.
4. OpenAI generates the response using the agent prompt and retrieved context.
5. Sarvam Bulbul v3 streams μ-law audio back to Twilio.
6. VAV stores transcript turns and runtime latency/usage metrics on the call.

The runtime clears Twilio's playback buffer and cancels the current TTS stream
when Sarvam reports speech start, providing barge-in behavior.

## Required serving configuration

- A Sarvam key saved in Workspace Settings or `SARVAM_API_KEY` on the API.
- `OPENAI_API_KEY` on the API.
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and a public HTTPS `BASE_URL`.
- The Twilio number's inbound voice webhook set to
  `POST {BASE_URL}/api/v1/webhooks/twilio/voice/inbound`.
- At least one E.164 number assigned in the agent's Runtime panel.
- The API ingress must support WebSocket upgrades at
  `/api/v1/realtime/twilio/{call_id}`.

Save the runtime policy, run **Test readiness**, and only then activate it.
Direct and inbound calls fail closed when the runtime is inactive or ambiguous.

## Knowledge migration

Text sources are immediately available to the VAV retriever. New PDF uploads
retain bounded locally extracted text as well as being uploaded to Smallest.ai.
PDFs uploaded before migration `20260830_014` have no local extracted content
and should be removed and re-uploaded before using them with a Sarvam agent.
Image-only PDFs need OCR before upload.

Website sources remain provider-indexed until VAV's controlled crawler stores
their page text locally. Do not describe a provider-only URL as available to a
Sarvam runtime.

## Etisalat SIP

Workspace Settings stores the Etisalat trunk and LiveKit credentials in the
same authenticated encrypted envelope used for provider keys. Credential
storage alone does not provision SIP/RTP infrastructure. The runtime deliberately
fails readiness until an external LiveKit SIP edge has created and verified the
trunk and dispatch rules and marked the gateway provisioned.

Railway hosts the HTTP/WebSocket control and media application. Do not expose a
self-hosted SIP service on the Railway web service: SIP signalling and the RTP
port range belong on LiveKit Cloud or a dedicated public VM/Kubernetes edge.

## Billing and budgets

VAV records provider-independent metered units (audio bytes, LLM tokens, turns)
and measured latency immediately. `Call.cost_cents` remains authoritative for
budget enforcement only after provider billing synchronization. The UI labels
unsynchronized runtime cost instead of presenting an invented estimate.

## Protocol references

- [Sarvam realtime STT](https://docs.sarvam.ai/api-reference/speech-to-text/transcribe/realtime/ws)
- [Sarvam streaming TTS](https://docs.sarvam.ai/api/api-guides-tutorials/text-to-speech/streaming-api/web-socket)
- [Twilio bidirectional Media Streams](https://www.twilio.com/docs/voice/media-streams/websocket-messages)
- [LiveKit SIP trunk setup](https://docs.livekit.io/telephony/start/sip-trunk-setup/)
- [LiveKit self-hosted SIP ports](https://docs.livekit.io/transport/self-hosting/sip-server/)
