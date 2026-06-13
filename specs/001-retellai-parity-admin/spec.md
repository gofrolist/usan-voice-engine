# Feature Specification: RetellAI-Parity Admin Console & Agent Studio

**Feature Branch**: `001-retellai-parity-admin`

**Created**: 2026-06-13

**Status**: Draft

**Input**: User description: "Update the admin UI and API to be functionally as close as possible to retellai.com — edit prompts, variables, change voice settings, change LLMs and their settings, etc. The current workflow to add variables/functions used in prompts is awkward (an 'unknown variable … will resolve to empty unless declared as a custom variable' warning forces a trip to a separate window to declare each one). Make variable management more UX-friendly (ideally declare them on the same screen). Defaults are confusing — it isn't clear what the default settings/profile are, and there's no way to edit the defaults themselves. Rework the 'elders' naming to be generic since customers aren't only elders. Don't limit changes to these items — get closer to RetellAI overall."

## Clarifications

### Session 2026-06-13

- Q: What generic term should replace "elder/elders" across the product? → A: Contacts
- Q: How far should broader RetellAI parity reach in THIS feature (beyond the explicitly-named editing improvements)? → A: Core editing parity + in-product agent testing/simulation (defer flow-builder, knowledge base, phone-number management, analytics dashboards)
- Q: How rich must voice selection be? → A: Searchable, curated voice list with metadata AND in-browser audio preview/playback of a sample
- Q: Does "edit defaults" mean a new editable system-baseline config, or the per-direction default profile? → A: The per-direction default profile is the only "default" (no separate editable baseline entity); the Defaults page surfaces and links to edit that profile, and the built-in last-resort fallback is shown read-only for transparency
- Q: How does the spoken (audio) agent test connect? → A: Browser webcall over WebRTC only (no real PSTN call placed, no phone number consumed) — matches RetellAI "Test Audio"
- Q: What data and providers do test sessions use? → A: Live LLM/TTS/STT (faithful to production, real provider cost) but populated solely from synthetic/admin-supplied sample values — no real contact PHI is ever auto-loaded into a test
- Q: Is the curated voice/model catalog editable in the admin console? → A: No — it is platform/engineer-maintained configuration; admins select from it but cannot add/remove catalog entries in-product (an in-product catalog editor is out of scope)
- Q: How are concurrent edits to the same profile draft handled? → A: Optimistic concurrency — a save against a draft that changed since it was loaded is blocked with a warning to reload, so no edits are silently overwritten

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Declare and manage variables without leaving the prompt (Priority: P1)

An administrator is editing an agent profile's system prompt. They type a token such as `{{state}}` or `{{med_name}}`. The editor immediately flags it as undeclared, and the administrator can declare it — name, description, example, and whether it carries protected health information (PHI) — directly inline, then continue editing. They never have to open a separate page, lose their place, or re-find which tokens were missing.

**Why this priority**: This is the single most-cited pain point. Today the editor warns "unknown variable: phone, on_dnc_list, state, time, med_name, offer_early_payment, first_call_time_local — will resolve to empty unless declared as a custom variable," and the only remedy is to navigate to a separate Variables page and add each one by hand, then return. Fixing this removes the most frequent, most disruptive friction in the core authoring loop.

**Independent Test**: Open a profile, type several undeclared tokens into the system prompt, and confirm each can be declared inline (with description/example/PHI) so the warning clears — all without navigating away — and the declared variables then appear in the shared variable catalog.

**Acceptance Scenarios**:

1. **Given** a prompt containing `{{state}}` that is not yet declared, **When** the administrator views the undeclared-variable warning, **Then** they are offered a one-action way to declare `{{state}}` in place (capturing name, description, example, PHI flag).
2. **Given** the administrator declares a previously-undeclared variable inline, **When** they confirm, **Then** the warning for that token clears immediately and the token renders as a recognized variable.
3. **Given** several undeclared tokens in one prompt, **When** the administrator chooses to declare them, **Then** they can declare each (or all remaining) without leaving the editing screen.
4. **Given** an administrator browsing available variables while writing a prompt, **When** they open the variable picker, **Then** they see built-in and custom variables grouped and labeled, with PHI variables clearly marked, and can insert one at the cursor.
5. **Given** a variable that is referenced by one or more prompts, **When** an administrator attempts to delete it, **Then** they are warned where it is still used before the deletion is allowed to proceed.
6. **Given** a token whose name collides with a reserved/built-in variable, **When** the administrator tries to declare it as custom, **Then** the system prevents the collision and explains why.

---

### User Story 2 - Choose voices, models, and their settings from guided pickers (Priority: P2)

An administrator wants to change how an agent sounds and thinks. Instead of typing raw identifiers into free-text fields, they pick a voice from a searchable, curated list (with language/gender/style metadata and an in-browser sample they can play), choose an LLM and a speech-to-text model from curated lists, and adjust the associated settings (e.g., speaking speed, temperature, language) with controls that show valid ranges and current defaults.

**Why this priority**: The user explicitly wants to "change voice settings, change LLMs and its settings." Today these are editable but only as free-text identifier fields, which is error-prone and undiscoverable — an administrator cannot tell which voices or models are valid, what each sounds like, or what a setting does. Guided pickers bring the experience to RetellAI's level and prevent invalid configurations from being published.

**Independent Test**: Open a profile, change the voice via a searchable picker and hear a sample, switch the LLM and STT models from curated lists, adjust temperature/speed/language, save and publish, and confirm the agent uses the selected voice and models on a test/live call.

**Acceptance Scenarios**:

1. **Given** the Voice section, **When** the administrator opens the voice picker, **Then** they can search/filter a curated list of supported voices with metadata (e.g., language, gender, style) and play a short audio sample of each before selecting.
2. **Given** the LLM section, **When** the administrator opens the model picker, **Then** they select from a curated list of supported models and adjust model settings (e.g., temperature) within validated ranges, with defaults visibly indicated.
3. **Given** the speech-to-text section, **When** the administrator opens the model picker, **Then** they select from a curated list of supported transcription models and set the recognition language.
4. **Given** an administrator selects a voice/model and saves, **When** the profile is published and a call runs, **Then** the call uses exactly the selected voice and models.
5. **Given** an invalid or unsupported selection (e.g., a voice/model no longer offered), **When** the administrator tries to save, **Then** the system blocks the save with a clear, field-level explanation.
6. **Given** a setting with a defined valid range, **When** the administrator enters a value outside it, **Then** the control prevents or rejects the value and explains the allowed range.

---

### User Story 3 - Understand and edit what runs by default (Priority: P3)

An administrator opens the Defaults area and immediately understands which agent configuration handles a call when no specific profile is assigned — for inbound calls, for outbound/scheduled calls. The per-direction default **profile** is the single source of truth for "what runs by default": the administrator can both (a) choose which profile is the default for each direction and (b) jump straight to editing that profile's configuration from the Defaults area. The page explains, in plain language, the resolution order (per-call override → per-contact assignment → per-direction default → built-in fallback) and shows the built-in last-resort fallback configuration read-only so the administrator understands what happens if no default is set.

**Why this priority**: The user says they "can't understand what is the default settings and profile" and want "an ability to edit defaults as well." Today the Defaults page only lets an administrator pick an inbound and an outbound profile from a dropdown, with no visibility into what actually runs and no obvious path to edit it. Treating the default profile as the editable source of truth (and exposing the built-in fallback read-only) removes the confusing dual concept and prevents calls from silently running with unexpected configuration.

**Independent Test**: Open Defaults, read the explanation of resolution order, set inbound and outbound default profiles, follow the link to edit the chosen default profile, save/publish, and confirm that a call with no per-contact assignment uses that profile's configuration.

**Acceptance Scenarios**:

1. **Given** the Defaults area, **When** an administrator views it, **Then** it clearly states, per call direction, which profile is currently the default and what is used if none is set.
2. **Given** the Defaults area, **When** an administrator views it, **Then** the resolution order (per-call override → per-contact assignment → per-direction default → built-in fallback) is explained in plain language, and the built-in fallback configuration is shown read-only.
3. **Given** an administrator with permission, **When** they select a default profile for a direction, **Then** only eligible (active, published) profiles are selectable and the choice is saved and audited.
4. **Given** a per-direction default profile is set, **When** an administrator follows the Defaults area's link to edit it, **Then** they are taken to that profile's editor, and published changes take effect as the default for that direction.
5. **Given** a profile currently set as a default, **When** it is archived or unpublished, **Then** the Defaults area surfaces that the default is no longer effective and prompts the administrator to choose a valid one.

---

### User Story 4 - Generic naming for call recipients (Priority: P3)

Throughout the admin console and the documented external surfaces, the domain term "elder/elders" is replaced with the generic term **"contact/contacts,"** so the product reads correctly for any customer segment, not only elder care. Every label, navigation item, page title, help text, and column header an administrator can see uses the generic term. Existing external integrations continue to work unchanged.

**Why this priority**: The user is expanding beyond elder-care customers and the eldercare-specific vocabulary is now incorrect and off-putting for other segments. Renaming the user-visible surfaces is high-value and low-risk; it is P3 (rather than higher) because it is independent of the editing-experience improvements and must be done without breaking live integrations.

**Independent Test**: Walk every admin screen and confirm no user-facing occurrence of "elder/elders" remains (navigation, titles, help text, column headers, empty states); separately confirm that existing external API calls and webhook consumers that use the prior field/route names still succeed.

**Acceptance Scenarios**:

1. **Given** any admin screen, **When** an administrator reads its labels, navigation, titles, help text, and column headers, **Then** no user-facing instance of "elder/elders" appears; the generic term is used consistently.
2. **Given** an existing external integration that creates or references call recipients using the prior naming, **When** it calls the system after the rename, **Then** it continues to succeed (backward compatibility is preserved).
3. **Given** historical data and previously published profile versions that embed the prior recipient-name token, **When** they are rendered or replayed, **Then** they continue to resolve correctly with no change in behavior.
4. **Given** the variable catalog, **When** an administrator browses it, **Then** a generically-named recipient-name variable is available for new prompts while the legacy token continues to function.

---

### User Story 5 - Test an agent before publishing (Priority: P4)

Before publishing a profile, an administrator can try it from within the console — either by exchanging messages with the agent's language model (text simulation) or by holding a short spoken test as a browser webcall (a WebRTC session in the browser, not a real phone call) — to confirm the prompt, variables, voice, and tools behave as intended. They can supply sample values for variables for the test run.

**Why this priority**: RetellAI's "Test Audio / Test LLM / Run Test" is a flagship capability that closes the authoring loop: change → try → fix → publish. It materially reduces the risk of publishing a broken prompt to live calls. It is P4 because the editing and clarity improvements (P1–P3) deliver value on their own and are prerequisites for a meaningful test.

**Independent Test**: Open a profile draft, start a test session, provide sample variable values, exchange a few turns (and/or a short spoken test), and observe the agent responding per the draft configuration without affecting production call records.

**Acceptance Scenarios**:

1. **Given** a profile draft, **When** an administrator starts a text test, **Then** they can exchange turns with the agent using the draft's prompt, variables, and tools.
2. **Given** a test session, **When** the administrator supplies sample values for variables, **Then** those values are substituted for the test run only, and no real contact's PHI is auto-loaded into the test.
3. **Given** a test session, **When** it runs, **Then** it does not create or alter production call, wellness, or audit records, and any side-effecting tools are clearly sandboxed or disabled.
4. **Given** a spoken (audio) test option, **When** the administrator runs it, **Then** a browser webcall (WebRTC) opens in which they hear the selected voice and can speak/respond for a short, bounded test, with no real phone call placed and no phone number consumed.

---

### Edge Cases

- **Undeclared variable cleanup**: A token is declared inline, then later removed from all prompts — the catalog entry remains until explicitly deleted; deletion warns if still referenced anywhere.
- **PHI in spoken-first content**: A PHI-flagged variable is inserted into content spoken before the recipient's identity is confirmed (greeting, recording disclosure, voicemail, goodbye) — the system warns (and blocks where carrier-visible, e.g., SMS bodies).
- **Default points at an ineligible profile**: A default profile is archived/unpublished — the system must not silently fall through without surfacing it.
- **Curated list drift**: A previously-selected voice/model is removed from the supported list — existing published profiles keep working, but editing surfaces the deprecation and blocks re-saving with the now-unsupported value.
- **Rename backward compatibility**: External callers and webhook consumers still send/expect the prior recipient naming — these must keep working during and after the rename.
- **Variable name collision**: An administrator declares a custom variable whose name matches a reserved/built-in one — must be prevented with explanation.
- **Concurrent edits**: Two administrators edit the same draft, or one publishes while another edits — handled via optimistic concurrency: a save against a draft that changed since it was loaded is blocked with a warning to reload, so no edits are silently overwritten.
- **Permission boundaries**: A viewer (read-only) opens the editor, the variable picker, the defaults page, or a test session — mutating actions are unavailable, reads are allowed.
- **Test with side-effecting tools**: A test run invokes a tool that would normally send an SMS, schedule a callback, or end a call — must be sandboxed so no real-world side effect occurs.

## Requirements *(mandatory)*

### Functional Requirements

**Variable management (P1)**

- **FR-001**: The prompt editor MUST detect tokens that are not declared in the variable catalog and indicate them non-destructively (without blocking continued editing).
- **FR-002**: For each undeclared token, the editor MUST offer an inline action to declare it as a custom variable — capturing name, description, example, and PHI flag — without navigating away from the editing screen.
- **FR-003**: Upon inline declaration, the undeclared indicator for that token MUST clear immediately and the token MUST be treated as recognized.
- **FR-004**: When multiple tokens are undeclared, the administrator MUST be able to declare each (and have an option to act on all remaining) from the editing screen.
- **FR-005**: The editor MUST provide a browsable variable picker that groups built-in and custom variables, clearly marks PHI variables, and inserts a selected variable at the cursor.
- **FR-006**: The system MUST prevent declaring a custom variable whose name collides with a reserved/built-in variable, with a clear explanation.
- **FR-007**: When deleting a custom variable, the system MUST warn the administrator if it is still referenced by any prompt or template before allowing the deletion.
- **FR-008**: Custom variables declared inline MUST appear in the shared variable catalog used across all profiles and prompt fields.

**Voice, LLM, and STT selection (P2)**

- **FR-009**: The Voice section MUST present a searchable, curated list of supported voices with metadata (at minimum language; plus gender/style where available) instead of a free-text identifier field.
- **FR-010**: The Voice picker MUST allow the administrator to play a short audio sample of a voice before selecting it.
- **FR-011**: The LLM section MUST present a curated list of supported language models for selection instead of a free-text identifier field.
- **FR-012**: The STT section MUST present a curated list of supported transcription models for selection instead of a free-text identifier field.
- **FR-013**: The system MUST allow editing model/voice settings (e.g., speaking speed, temperature, recognition/synthesis language) using controls that enforce valid ranges and visibly indicate defaults.
- **FR-014**: The system MUST block saving a profile that references an unsupported/withdrawn voice or model, with a clear field-level explanation, while NOT breaking already-published versions that reference it.
- **FR-015**: A published profile's selected voice and models MUST be the ones used on live and test calls for that profile.

**Defaults clarity and editing (P3)**

- **FR-016**: The Defaults area MUST show, per call direction (inbound and outbound), which profile is currently the default and what happens if none is set.
- **FR-017**: The Defaults area MUST explain, in plain language, the configuration resolution order: per-call override → per-contact assignment → per-direction default → built-in fallback, and MUST display the built-in last-resort fallback configuration read-only.
- **FR-018**: Administrators MUST be able to select the default profile for each direction, restricted to eligible (active, published) profiles, with the change audited.
- **FR-019**: The Defaults area MUST provide a direct path to edit the per-direction default profile (the editable source of truth for "what runs by default"); there is no separate editable system-baseline entity, and the built-in fallback is not editable.
- **FR-020**: When a profile set as a default becomes ineligible (archived/unpublished), the Defaults area MUST surface that the default is no longer effective and prompt for a valid replacement.

**Generic naming (US4)**

- **FR-021**: All user-facing occurrences of "elder/elders" across the admin console (navigation, page titles, headings, help text, column headers, empty states, button labels) MUST be replaced with the generic term "contact/contacts."
- **FR-022**: The rename MUST preserve backward compatibility for existing external integrations: prior route names, request/response field names, and webhook payload field names that external systems depend on MUST continue to function.
- **FR-023**: Previously published profile versions and historical data that embed the legacy recipient-name token MUST continue to resolve and behave identically after the rename.
- **FR-024**: A generically-named recipient-name variable MUST be available in the catalog for authoring new prompts, alongside the still-functioning legacy token.

**Agent testing / simulation (P4)**

- **FR-025**: Administrators MUST be able to start a text-based test of a profile draft, exchanging turns with the agent using the draft's prompt, variables, and tools. Test sessions MUST invoke the live language model (and, for audio tests, live speech/voice services) so behavior is faithful to production.
- **FR-026**: Administrators MUST be able to supply sample values for variables that apply to the test run only; test sessions MUST be populated solely from synthetic/admin-supplied values and MUST NOT auto-load any real contact's PHI.
- **FR-027**: Test sessions MUST NOT create or modify production call, wellness, medication, or audit records, and side-effecting tools MUST be sandboxed or disabled during a test.
- **FR-028**: Administrators MUST be able to run a short, bounded spoken (audio) test conducted as a browser webcall over WebRTC — using the selected voice — that places no real PSTN call and consumes no phone number.

**Cross-cutting**

- **FR-029**: All configuration changes (variables, voice/model selections, defaults, baseline settings) MUST follow the existing draft → publish → version-history model, with auditing of who changed what and when.
- **FR-030**: Read-only (viewer) users MUST be able to view all of the above but MUST NOT be able to mutate variables, selections, defaults, or run side-effecting test actions.
- **FR-031**: Validation errors from any save MUST be presented at the relevant field/control with a clear, human-readable message.
- **FR-032**: Concurrent edits to a profile draft MUST be handled with optimistic concurrency: a save against a draft that has changed since it was loaded MUST be rejected with a clear warning prompting the editor to reload, so no administrator's changes are silently overwritten.

### Key Entities *(include if feature involves data)*

- **Agent Profile**: A named, versioned bundle of agent configuration (prompts, voice, models, timing, tools, voicemail, speech, policy). Has a draft and zero or more immutable published versions; may be marked as a per-direction default.
- **Profile Version**: An immutable snapshot of a profile's configuration at publish time; what live and test calls actually use.
- **Variable (Built-in / Custom)**: A `{{token}}` placeholder usable in prompts and templates. Built-ins are system-provided (some carry PHI); customs are administrator-declared documentation entries (name, description, example, PHI flag) carrying no stored value. Substituted per call.
- **Voice**: A selectable, platform-curated synthesis voice with metadata (language, gender, style) and a playable sample; referenced by a profile. The catalog is platform-maintained, not admin-editable.
- **Model (LLM / STT)**: A selectable, platform-curated language or transcription model with associated settings (e.g., temperature, language); referenced by a profile. The catalog is platform-maintained, not admin-editable.
- **Default Assignment**: The mapping of each call direction (inbound/outbound) to a default profile. There is no separate editable baseline; the default profile is the editable source of truth, and a read-only built-in configuration serves only as the last-resort fallback when no default profile resolves.
- **Contact (formerly "Elder")**: A call recipient — name, phone, timezone, optional assigned profile, metadata. The domain entity being renamed in user-facing surfaces.
- **Tool / Function**: A capability an agent can invoke during a call (e.g., logging, scheduling, messaging, ending the call); enabled per profile, some requiring configuration.
- **Test Session**: A non-production, sandboxed run of a profile draft — text simulation and/or a short browser webcall (WebRTC, no PSTN) — that invokes live LLM/TTS/STT but is populated only with synthetic/admin-supplied sample variable values (never real contact PHI), used to validate behavior before publishing.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An administrator can declare a previously-undeclared variable and clear its warning entirely within the prompt-editing screen, with zero navigations to another page.
- **SC-002**: The number of distinct screens/navigations required to author a prompt with three new variables drops from the current multi-page flow to a single screen.
- **SC-003**: An administrator can select a voice and hear a sample of it before saving, in under 30 seconds, without typing any identifier.
- **SC-004**: 100% of voice/LLM/STT selections are made from curated lists; it is not possible to publish a profile referencing an unsupported voice or model.
- **SC-005**: From the Defaults area alone, an administrator can correctly state which configuration runs for an unassigned inbound call and for an unassigned outbound call.
- **SC-006**: An administrator can change which profile is the default for a direction (or edit that default profile) and confirm the change takes effect for a call with no per-contact assignment.
- **SC-007**: Zero user-facing occurrences of "elder/elders" remain on any admin screen after the rename.
- **SC-008**: 100% of existing external integrations and webhook consumers continue to succeed after the rename (no breaking changes to depended-upon route/field/payload names).
- **SC-009**: An administrator can test a draft profile (text and/or short audio) and observe expected behavior without producing any production call/wellness/audit record.
- **SC-010**: All in-scope configuration changes remain captured by the existing draft/publish/version-history and audit mechanisms (no change loses traceability).
- **SC-011**: When two administrators edit the same draft, the second save is blocked with a reload warning rather than silently overwriting the first — zero silent lost updates.

## Assumptions

- **Rename strategy is shim-first (backward compatible)**: Based on the repository's own tenancy research, the rename changes user-facing surfaces (labels, navigation, help text) and adds generically-named aliases, while keeping the underlying persisted schema, external route/field names, webhook payload keys, and the built-in recipient-name token physically intact for backward compatibility. A deep physical rename of database tables/columns and external contract field names is OUT OF SCOPE for this feature.
- **Scope tier**: This feature covers core editing parity (prompts, variables, voice, LLM, STT, defaults) plus in-product agent testing/simulation. Explicitly OUT OF SCOPE for this feature: a visual conversation-flow builder, a knowledge-base management UI, phone-number management, batch-call authoring UI, contacts CRUD UI, and analytics/live-monitoring/AI-QA/alerting dashboards. These may be considered in follow-up features.
- **Curated catalogs are platform-maintained (not admin-editable)**: The set of supported voices and models is maintained as platform/engineering configuration (each must also be wired into the agent runtime). Administrators choose from it rather than supplying arbitrary identifiers, and cannot add or remove catalog entries from within the console.
- **Existing capabilities are retained**: The current draft/publish/version-history, rollback, custom-variable PHI handling, tool catalog, timing, voicemail, speech-advanced, and policy (TCPA/quiet-hours) controls remain available and are not regressed.
- **Audio sample playback**: A short, representative audio sample is available (or generatable) for each curated voice for in-browser preview.
- **Permissions model unchanged**: The existing admin/viewer role split and SSO allow-list continue to govern who can mutate configuration.
- **Single-tenant scope**: This feature does not introduce multi-tenancy; it prepares vocabulary to be tenant-neutral but does not implement per-tenant isolation.

## Out of Scope

- Visual conversation-flow / node-graph agent builder (RetellAI "Conversation Flow" agent type).
- Knowledge-base ingestion and management UI.
- Phone-number purchasing/assignment management UI.
- Batch-call and schedule authoring UI (the underlying capabilities exist in the API; this feature does not add their console surfaces).
- Analytics, live-monitoring, AI quality-assurance, and alerting dashboards.
- In-product catalog-management UI for adding/removing supported voices or models (the catalog is platform/engineer-maintained configuration).
- Deep physical rename of database schema, external API field names, and webhook payload keys.
- Multi-tenant authentication/authorization and per-tenant data isolation.
