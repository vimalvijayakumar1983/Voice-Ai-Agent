# Production design and interaction QA

Date: 2026-08-28  
Environment: Railway production  
Frontend: `https://frontend-production-ce23.up.railway.app`  
API readiness: `https://api-production-db032.up.railway.app/ready`

## Release evidence

- GitHub Actions run 13 completed successfully for both frontend and backend.
- The backend gate ran the real PostgreSQL migration, Ruff, format checks, and the complete pytest suite.
- The frontend gate ran dependency audit, 40 interaction/security tests, lint, TypeScript, and the production build.
- The live API readiness probe returned `200` with schema-ready version `0.3.0`.
- The deployed frontend's `/api/v1/auth/registration-policy` rewrite returned the backend policy with `200`.
- Production CSP uses a per-request nonce and limits browser connections to the frontend origin plus the exact Smallest.ai websocket origin.

## Browser verification

- Securely signed in as the existing workspace administrator.
- Confirmed that the authenticated session survives a browser reload and ten consecutive hard navigations.
- Opened Overview, Agents, Playground, Conversations, Workflows, Campaigns, Compliance, Integrations, Usage & billing, and Settings.
- Confirmed a unique descriptive title, signed-in workspace shell, meaningful loading/empty state, and no application-origin console errors on every route.
- Confirmed that Workflows, planned integrations, and billing clearly disclose their current operational limits instead of implying unavailable functionality.
- Did not start a provider session, request microphone permission, preview a voice, fetch a recording, place a call, publish an agent, or create external data during QA.

## Voice and multilingual verification

- Live catalog loaded 234 usable voices, 20 provider-reported languages, and five built-in templates.
- Applied the AI Receptionist template and confirmed that its fields remain editable.
- Added English, Tamil, and Hindi to one unsaved draft.
- Confirmed automatic same-call switching becomes selected for the multilingual draft and includes an explicit Playground validation warning.
- Confirmed 111 voices cover the full English + Tamil + Hindi set.
- Selected Anuja and confirmed `3/3 ready` coverage without saving the draft.
- Searched the catalog to one exact result, changed availability scope, paged to incompatible results, and confirmed voices missing Tamil/Hindi are disabled.
- Confirmed stored voices absent from the current catalog are preserved and labeled `not in current catalog` rather than silently replaced.

## Visual comparison set

Each comparison contains the captured reference and the deployed implementation in one image and was inspected at the same desktop state and near-identical viewport.

| Flow | Reference | Production | Combined comparison | Result |
| --- | --- | --- | --- | --- |
| Overview | `../audit/02-overview.jpg` | `../audit/implementation-overview.jpg` | `../audit/comparison-overview.jpg` | Passed; operational claims are now evidence-based. |
| Agent list | `../audit/03-agents.jpg` | `../audit/implementation-agents.jpg` | `../audit/comparison-agents.jpg` | Passed; filters and provider revision state improve scanability. |
| Playground | `../audit/01-playground.jpg` | `../audit/implementation-playground.jpg` | `../audit/comparison-playground.jpg` | Passed; validation scenario and language readiness are explicit. |
| Compliance | `../audit/12-compliance.jpg` | `../audit/implementation-compliance.jpg` | `../audit/comparison-compliance.jpg` | Passed; append-only consent evidence and suppression controls are clear. |
| Settings | `../audit/15-settings.jpg` | `../audit/implementation-settings.jpg` | `../audit/comparison-settings.jpg` | Passed; team, invitation, credential, and audit surfaces remain consistent. |
| Languages | `../audit/05-agent-create-languages.jpg` | `../audit/implementation-agent-languages.jpg` | `../audit/comparison-agent-languages.jpg` | Passed; exact same-call mode and per-language readiness are visible. |
| Voice library | `../audit/07-incompatible-voice-selected.jpg` | `../audit/implementation-voice-library.jpg` | `../audit/comparison-voice-library.jpg` | Passed; compatible, missing-language, unavailable, and preview states are unambiguous. |

## Responsive and accessibility checks

- Desktop browser screenshots were verified at 1348×926 or 1363×936, matching the supplied reference captures.
- Automated shell tests cover the mobile navigation focus trap, inert background, Escape close behavior, and focus wrapping.
- The live shell exposes a skip link, landmark regions, labeled navigation, semantic headings, form labels, status/alert regions, and descriptive control names.
- Loading states were excluded from final comparisons; screenshots were captured only after their screen-specific loaded signal became visible.

## Intentional differences

- The previous Tamil restriction was removed because the current provider catalog and Lightning v3.1 metadata support Tamil and automatic multilingual detection. The UI still requires exact catalog coverage and instructs operators to validate the language combination before activation.
- Overview readiness copy no longer claims end-to-end production quality from configuration alone.
- The agent list, Playground, compliance, and voice library are intentionally denser because they now expose validation, revision, consent, and compatibility evidence that was previously absent.

final result: passed
