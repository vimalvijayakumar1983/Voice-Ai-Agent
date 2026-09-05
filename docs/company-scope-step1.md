# Explicit company scope — Step 1

This is an opt-in VAV Inworld single-pass rollout. It does not replace the knowledge
compiler, add semantic interpretation, or change hosted Smallest/Sarvam agents.

## Configuration

The agent editor exposes `knowledge_company_scope` through the existing tenant-scoped
agent create/update API. It is stored within agent metadata, preserving other keys;
no database migration is needed. Example:

```json
{
  "default_company": "Example Group",
  "companies": [
    {"name": "Example Group", "aliases": ["the group", "head office"]},
    {"name": "Example Trading", "aliases": ["trading"]}
  ]
}
```

Company names must match the approved facts' subjects. Aliases select the canonical
company only; they do not confer access to another KB or tenant. An absent default
requires clarification. An absent scope preserves legacy behaviour for staged rollout.
The currently admitted tenant/KB/revision remains the retrieval authority.

## Runtime contract

- A new runtime starts with the explicitly configured default, never an agent-title guess.
- Exact names and configured aliases update the selected company. Ambiguous selections
  clear the old scope and request clarification. This is deterministic, not semantic NLU.
- Exact and general retrieval both filter structured facts by canonical subject. Raw
  source text without explicit subject attribution is excluded in scoped mode, including
  fallback paths and KB-level text. A footer mention is not company ownership.
- The exact index is filtered through an immutable copy, never by mutating the shared cache.
- Company changes clear previous questions and repeat memory. A response callback captures
  its company epoch; delayed speech cannot contaminate a new company's conversation.
- Repeat memory uses LiveKit's speech-handle committed transcript, not a retrieval result
  or unspoken generated text. Partial interrupted speech is repeated only as committed.
  These are provider-observed words, not proof of playback at the caller's device.
- Typed repeat requests do not fall back to an unrelated fact type. Slow repeats separate
  digit runs while retaining country-code punctuation and extension boundaries.
- No additional LLM/model API call is introduced.

## Release checks

`test_conversation_scope.py` runs cold-session phone questions, real database retrieval,
company corrections, ambiguous/unknown scope, two independent companies, API persistence,
unattributed raw-source exclusion and stale/interrupted committed-speech handling.

Before enabling another agent, inspect its published subject names, configure its scope,
and run fresh-session tests against that agent's actual published revision. Do not infer
all source ownership from one company name, and do not mark uncompiled sources ready.

## Remaining work (not claimed by this release)

Semantic paraphrase handling, complete company attribution for raw/legacy source pages,
automatic source-to-company selection in the editor, branch/person relationship retrieval,
and the hosted-provider lanes need separate validation/work. A safe scoped miss is not
evidence that the fact is absent from the original website. No audio quality/latency gain
is claimed by deterministic retrieval tests.

Rollback: set the agent's `knowledge_company_scope` to null through its edit API. New
calls use the prior behaviour; running runtime objects retain their copied configuration.
