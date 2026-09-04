# Audio replay canaries

These manifests replay a local caller-only WAV or signed 16-bit PCM fixture into
an explicitly allowlisted QA agent. They never contain provider credentials,
tenant identifiers, production agent identifiers, telephone numbers, or audio.

Run a schema-only dry run from `backend`:

```powershell
python scripts/replay_audio_canary.py tests/quality/manifests/real_kb_qa_replay.example.json
```

Set every environment variable listed by the dry run. The real-KB clone's agent
name must contain `qa`, `test`, or `canary`, and its UUID must also appear in
`VAV_QA_REPLAY_ALLOWED_AGENT_IDS`. Then authorize exactly one live replay:

```powershell
python scripts/replay_audio_canary.py tests/quality/manifests/real_kb_qa_replay.example.json --confirm-live
```

The command exits `0` on pass, `1` on a recognition/latency/grounding regression,
and `2` on configuration or execution failure. Its JSON report contains hashes,
counts, aggregate latency and bounded turn diagnostics, but never transcript text,
audio paths, bearer tokens, LiveKit tokens, or telephone numbers.

The SIP form only joins a pre-created room and requires both an allowlisted QA
agent and an allowlisted SIP fixture ID. It cannot originate a SIP call.

The interruption example expects two final caller turns from a timed audio
fixture, at least one barge-in, no spurious fragment suppression, and interruption
detection within 250 ms. This makes endpointing regressions fail the same CI gate
as proper-name recognition, latency, and grounding regressions.
