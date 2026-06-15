# Feature Specification: Clara Care Parity — Closing the RetellAI Behavioral Gap

**Feature Branch**: `002-clara-care-parity`

**Created**: 2026-06-14

**Status**: Draft

**Input**: User description: "Review current functionality and compare with what we already have configured via retellai.com; we want to migrate from it to our own system. Create the missing functions." (Capability list provided in Russian, covering daily care, personal memory, health & medications, safety & crises, family connection, calls & scheduling, and subscription & sales.)

## Context: Why This Feature Exists

USAN Retirement runs a daily wellness companion ("Clara") that today is configured and operated through **RetellAI**. The goal is to migrate off RetellAI onto our own self-hosted voice engine without losing any behavior elders, families, and operators rely on.

A capability-by-capability audit of the current codebase (`apps/api` + `services/agent`) against the RetellAI configuration revealed that several behaviors are **fully implemented**, several are **partial**, and several are **entirely missing**. This specification defines the **missing and partial functions** required to reach behavioral parity with RetellAI.

### Audit Summary (current state → gap)

| Capability | Current State | This Spec |
|---|---|---|
| Morning outbound wellness calls + scheduling | Implemented | — |
| Evening calls (second daily window, toggleable) | Missing | **In scope** |
| Cross-call memory (yesterday's mood, plans, topics) | Partial (last log only) | **In scope** |
| Structured personal-facts memory (family, routines, dates, health) | Partial (unstructured metadata) | **In scope** |
| Medication reminders / mark taken | Implemented | — |
| Medication re-reminder when not taken | Missing | **In scope** |
| Mood/wellbeing logging per call | Implemented | — |
| Monthly wellbeing survey (loneliness, mood, satisfaction) | Missing | **In scope** |
| Mood-boosting activities (breathing, memory games), non-repeating | Missing | **In scope** |
| Crisis detection (suicidal, medical, abuse, confusion, overdose) | Partial (LLM-only flag, no safety net, no resources) | **In scope** |
| Emergency resources (988, 911, APS, Poison Control) + family alert | Missing | **In scope** |
| Anti-scam warnings / red-flag education | Missing | **In scope** |
| DNC / opt-out via spoken "stop" or SMS STOP | Partial (manual list only) | **In scope** |
| Family SMS task intake → delivered next call → closed | Missing | **In scope** |
| Family alerts on missed calls and crises | Missing | **In scope** |
| Monthly family report on status/trends | Missing | **In scope** |
| Inbound call handling (recognize client, recall context) | Implemented | — |
| Callback on request, auto-dialed ("call me in an hour") | Partial (request logged, no auto-dial) | **In scope** |
| Outbound informational SMS with helpful numbers | Partial (templates exist; no resource numbers) | **In scope** |
| Spanish-language handling (promise + Spanish callback) | Missing | **In scope** |
| Subscription / trial signup + FAQ | Missing | **Deferred to spec 003** |
| Phone payment (DTMF card via Stripe / payment link) | Missing | **Deferred to spec 003** |

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Crisis detection and emergency escalation (Priority: P1)

During any call, if the elder expresses a life-safety crisis — suicidal thoughts, a medical emergency, signs of elder abuse, acute confusion, or a poisoning/overdose — Clara must reliably recognize it, respond with the correct emergency resource, and escalate to the family/operator, even if the conversational model alone fails to catch it.

**Why this priority**: This is the highest-stakes behavior in an elder-care product. A missed crisis can mean a missed life. It must work before any softer feature is migrated.

**Independent Test**: Run scripted calls containing each crisis type (and clear non-crisis controls). Verify that (a) the correct resource (988 / 911 / Adult Protective Services / Poison Control) is offered, (b) an urgent escalation record is created, (c) the family contact receives an alert, and (d) a deterministic safety-net trigger fires when the model misses an explicit crisis phrase.

**Acceptance Scenarios**:

1. **Given** an elder on a call says they want to end their life, **When** the statement is spoken, **Then** Clara provides the 988 Suicide & Crisis Lifeline, creates an urgent escalation, and notifies the family contact.
2. **Given** an elder describes chest pain and difficulty breathing, **When** detected, **Then** Clara urges calling 911, offers to help, and raises an urgent medical escalation.
3. **Given** an elder describes being hit or financially exploited by a caregiver, **When** detected, **Then** Clara provides the Adult Protective Services pathway and raises an urgent safety escalation.
4. **Given** an elder reports swallowing the wrong pills or a double dose, **When** detected, **Then** Clara provides Poison Control (1-800-222-1222) and raises an urgent medical escalation.
5. **Given** the conversational model does not classify an explicit crisis phrase as a crisis, **When** the deterministic safety-net layer matches that phrase, **Then** the same escalation and resource-delivery path still fires.
6. **Given** a non-crisis statement that merely mentions a sensitive word in a benign context, **When** evaluated, **Then** no false escalation is raised (measured against a control set).

---

### User Story 2 - Family task relay and alerting (Priority: P2)

A family member can text Clara a short task ("remind mom to drink water"), and Clara delivers it naturally on the elder's next call and marks it done. Families are also alerted when a call is missed or a crisis is detected.

**Why this priority**: Family involvement is the core differentiator of the product and the second-most-valuable behavior after safety. It depends on a family-contact registry that several other stories also use.

**Independent Test**: Register a family contact, send an inbound SMS task, run the elder's next call, and confirm the task is spoken and then closed. Separately, force a missed call and a crisis flag and confirm the family contact receives each alert.

**Acceptance Scenarios**:

1. **Given** a registered family contact texts a task to Clara's number, **When** the message is received, **Then** an open family task is created for the linked elder.
2. **Given** an open family task exists, **When** the elder's next call runs, **Then** Clara conveys the task in natural language during the conversation.
3. **Given** Clara has conveyed the task, **When** the call ends, **Then** the task is marked delivered/closed and not repeated on the following call.
4. **Given** an outbound call goes unanswered after the configured retry policy, **When** the call is finalized as missed, **Then** the family contact receives a missed-call alert.
5. **Given** an urgent crisis escalation is raised (US1), **When** it is created, **Then** the family contact receives a crisis alert.
6. **Given** an inbound message from an unregistered number, **When** received, **Then** no task is created and the sender is handled per a safe default (ignored/queued for operator review).

---

### User Story 3 - Medication adherence with re-reminders (Priority: P2)

Clara reminds the elder of the medications due, records whether each was taken, and — when one was not taken — sets and delivers a re-reminder later in the same call or on a follow-up touch, without nagging endlessly.

**Why this priority**: Medication reminders already exist; the missing re-reminder loop is a meaningful adherence improvement and a stated RetellAI behavior.

**Independent Test**: Run a call where the elder says a medication was not taken; verify a re-reminder is scheduled and delivered, and that adherence is recorded across the cycle.

**Acceptance Scenarios**:

1. **Given** an elder has medications due, **When** the call runs, **Then** Clara reminds them of each due medication.
2. **Given** the elder says a medication was not taken, **When** recorded, **Then** Clara sets a re-reminder for that medication.
3. **Given** a re-reminder is due, **When** the next eligible touchpoint occurs, **Then** Clara re-asks about that specific medication.
4. **Given** the elder confirms the medication was taken, **When** recorded, **Then** the re-reminder is cleared and not repeated.
5. **Given** a re-reminder has fired the maximum configured number of times without confirmation, **When** the cap is reached, **Then** Clara stops re-asking and raises a routine follow-up instead of nagging.

---

### User Story 4 - Personalized memory across calls (Priority: P2)

Clara remembers personal facts about the elder — close people (son, daughter, neighbors), routines and habits, preferences, important dates, and health context — and remembers what was discussed and planned on prior calls, then uses these naturally in later conversations.

**Why this priority**: Continuity is what makes Clara feel like a companion rather than a robocaller. It is partially present (last wellness log only) and needs structured personal facts plus conversational carryover.

**Independent Test**: Seed an elder with personal facts and run two consecutive calls; verify the second call references prior facts and at least one item discussed/planned in the first call.

**Acceptance Scenarios**:

1. **Given** an elder has recorded personal facts (family names, routines, preferences, important dates, health context), **When** a call runs, **Then** Clara can reference the relevant facts naturally.
2. **Given** the elder mentions a new durable fact during a call ("my daughter Maria is visiting Sunday"), **When** the call ends, **Then** that fact is captured for future calls.
3. **Given** a prior call discussed a plan or intention ("I'm going to the doctor tomorrow"), **When** the next call runs, **Then** Clara can follow up on it.
4. **Given** an important date is near (e.g., a birthday or anniversary), **When** a call falls on or near it, **Then** Clara acknowledges it.
5. **Given** stored facts include health context, **When** used in conversation, **Then** they remain on PHI-compliant infrastructure and are never sent to a non-covered service.

---

### User Story 5 - Evening calls and schedule flexibility (Priority: P2)

Operators can configure both a morning and an evening call window per elder, and can turn the evening call on or off independently.

**Why this priority**: A stated RetellAI behavior currently blocked by the single-window-per-elder schedule model.

**Independent Test**: Configure an elder with morning + evening windows, confirm two calls materialize on enabled days; disable the evening window and confirm only the morning call materializes.

**Acceptance Scenarios**:

1. **Given** an elder with a morning and an evening window enabled, **When** scheduling runs on an active day, **Then** both a morning and an evening call are placed within their respective windows.
2. **Given** the evening window is disabled, **When** scheduling runs, **Then** only the morning call is placed.
3. **Given** both windows are configured, **When** quiet-hours / timezone rules apply, **Then** each call is placed only within its own local window.
4. **Given** the elder is on the DNC list, **When** scheduling runs, **Then** neither call is placed.

---

### User Story 6 - Wellbeing programs: monthly survey and mood-boosting activities (Priority: P3)

Once a month Clara runs a short structured wellbeing survey (loneliness, mood, satisfaction). On any call where the elder's mood is low, Clara offers a mood-boosting activity (a breathing exercise, a memory exercise, or a light game), choosing one that wasn't used recently.

**Why this priority**: Valuable engagement and outcome-tracking behaviors, but not safety- or continuity-critical for the initial cutover.

**Independent Test**: Trigger a survey-due elder and confirm the survey runs and is recorded once for the month. Separately, run a call with a low mood and confirm an activity is offered that differs from the most recent one used.

**Acceptance Scenarios**:

1. **Given** an elder is due for the monthly survey, **When** the next call runs, **Then** Clara administers the survey questions and records the structured results.
2. **Given** the survey was completed this month, **When** later calls run that month, **Then** the survey is not repeated.
3. **Given** the elder reports a low mood, **When** detected, **Then** Clara offers a mood-boosting activity.
4. **Given** Clara has used a specific activity recently, **When** offering another, **Then** it selects a different activity from the catalog.
5. **Given** the elder declines an activity, **When** they decline, **Then** Clara accepts gracefully and does not push.

---

### User Story 7 - Anti-scam education and opt-out handling (Priority: P3)

Clara proactively warns about common scams and explains red flags when relevant, and reliably honors opt-out requests — whether the elder says "stop calling" on a call or texts STOP to Clara's number.

**Why this priority**: Protective and compliance-relevant behaviors. DNC enforcement on outbound already exists; the missing piece is capturing opt-out from the elder's own words and inbound SMS.

**Independent Test**: Run a call where the elder reports a suspicious caller and confirm Clara explains the red flags. Separately, have the elder say "don't call me anymore" on a call and text STOP, and confirm both add the number to the do-not-call list and stop future outbound calls.

**Acceptance Scenarios**:

1. **Given** the elder describes a suspicious request (gift cards, wire transfer, "IRS" threats), **When** detected, **Then** Clara warns it is likely a scam and explains the red flags.
2. **Given** the elder says they no longer want calls, **When** detected on a call, **Then** the number is added to the do-not-call list and no further outbound calls are placed.
3. **Given** the elder texts STOP (or an equivalent opt-out keyword) to Clara's number, **When** received, **Then** the number is added to the do-not-call list.
4. **Given** a number is on the do-not-call list, **When** scheduling or batch runs, **Then** no outbound call is placed to it.
5. **Given** an opt-out is recorded, **When** it occurs, **Then** the family contact and/or operator is notified so they understand why calls stopped.

---

### User Story 8 - Callback auto-dial, Spanish callback, and monthly family report (Priority: P3)

When an elder asks to be called back ("call me in an hour / tomorrow"), Clara honors it with an actual scheduled call. If the elder speaks Spanish, Clara promises a Spanish-language callback and arranges it. Once a month, the family receives a report on the elder's status and trends.

**Why this priority**: Rounding-out behaviors that complete parity. Callback requests are already logged but not auto-dialed; Spanish handling and family reports are net-new.

**Independent Test**: Request a callback in one hour and confirm a call is actually placed at that time within rules. Trigger a Spanish-speaking caller and confirm a Spanish callback is promised and scheduled. Generate a monthly report for an elder with a month of data and confirm the family receives it.

**Acceptance Scenarios**:

1. **Given** the elder asks to be called back at a specific later time, **When** the request is captured, **Then** an outbound call is automatically placed at that time, subject to quiet-hours and DNC rules.
2. **Given** the requested callback time falls in quiet hours, **When** scheduling, **Then** the call is deferred to the next allowed time and the elder's expectation is set accordingly.
3. **Given** an elder speaks Spanish during a call, **When** detected, **Then** Clara promises a Spanish-language callback, records the language preference, and schedules a Spanish callback.
4. **Given** an elder has a month of call data, **When** the monthly report runs, **Then** a status-and-trends summary is generated and delivered to the family contact.
5. **Given** an elder has no family contact registered, **When** alerts or reports would be sent, **Then** they are routed to the operator queue instead and the absence is surfaced.

---

### Edge Cases

- **Crisis during voicemail / no human present**: deterministic safety-net and escalation must still fire on detected content, but resource delivery to a non-answering line is moot — escalation to family/operator takes precedence.
- **Multiple crises in one call** (e.g., confusion + medication overdose): all relevant resources and the highest-severity escalation are surfaced.
- **Family task that is unsafe or out of scope** (e.g., "tell mom to stop taking her heart pills"): tasks that conflict with medical safety must not be relayed verbatim and should be flagged for operator review.
- **Conflicting opt-out then re-consent**: a number removed from DNC by an operator after an opt-out must be auditable.
- **Evening + callback collision**: a requested callback that overlaps the evening window should not produce two near-simultaneous calls.
- **Survey due but elder in distress**: safety and crisis handling always preempt survey/activity flows.
- **Personal-fact contradictions**: a newly stated fact that contradicts a stored one (e.g., changed routine) must update rather than duplicate.
- **Spanish detected mid-call on an English call**: Clara should not abruptly switch languages but should set up the Spanish callback.
- **Family contact texts in a language other than English**: inbound task parsing must handle non-English text or safely defer to operator review.
- **Activity repetition exhaustion**: when the catalog of recently-unused activities is exhausted, the least-recently-used activity may be reused.

## Requirements *(mandatory)*

### Functional Requirements

#### Crisis safety (US1)

- **FR-001**: System MUST detect, within a call, the following crisis categories from the elder's speech: suicidal ideation, medical emergency, elder abuse, acute confusion, and poisoning/overdose.
- **FR-002**: Detection MUST use both the conversational model AND a deterministic safety-net layer (explicit phrase/keyword triggers) so that a model miss on an explicit crisis phrase still triggers escalation.
- **FR-003**: On a detected crisis, system MUST surface the correct emergency resource to the elder: 988 (suicide/crisis), 911 (medical emergency), Adult Protective Services (abuse), Poison Control 1-800-222-1222 (poisoning/overdose).
- **FR-004**: On a detected crisis, system MUST create an urgent escalation record and notify the elder's family contact (and operator queue) without delay.
- **FR-005**: System MUST minimize false escalations such that benign mentions of sensitive words do not trigger emergency flows (validated against a control set).
- **FR-006**: Emergency resource numbers MUST be maintained as configuration/data, not hard-coded into conversational prompts, so they can be updated without prompt edits.

#### Family connection (US2, US8)

- **FR-007**: System MUST allow operators to register one or more family contacts per elder, including phone number and relationship.
- **FR-008**: System MUST accept inbound SMS from a registered family contact and create an open family task linked to the corresponding elder.
- **FR-009**: System MUST convey open family tasks to the elder during the next call and mark them delivered/closed afterward, never repeating a closed task.
- **FR-010**: System MUST alert the family contact when an outbound call is finalized as missed (after the retry policy is exhausted).
- **FR-011**: System MUST alert the family contact when an urgent crisis escalation is raised.
- **FR-012**: System MUST generate and deliver a monthly per-elder status-and-trends report to the family contact.
- **FR-013**: When no family contact is registered, alerts and reports MUST route to the operator queue and the absence MUST be visible to operators.
- **FR-014**: Inbound messages from numbers not matched to a registered family contact MUST NOT create tasks and MUST be handled by a safe default (ignored or queued for operator review).
- **FR-015**: Family tasks that conflict with medical safety MUST be flagged for operator review rather than relayed verbatim.

#### Medications (US3)

- **FR-016**: System MUST set a re-reminder when the elder reports a due medication as not taken.
- **FR-017**: System MUST deliver the re-reminder at the next eligible touchpoint, referencing the specific medication.
- **FR-018**: System MUST clear a re-reminder once the elder confirms the medication was taken.
- **FR-019**: System MUST cap the number of re-reminders per medication per cycle and, on reaching the cap, raise a routine follow-up instead of continuing to re-ask.

#### Memory & personalization (US4)

- **FR-020**: System MUST store structured personal facts per elder: close people/relationships, routines/habits, preferences, important dates, and health context.
- **FR-021**: System MUST make relevant personal facts available to the conversation so Clara can reference them naturally.
- **FR-022**: System MUST capture durable new facts stated by the elder during a call for use in future calls.
- **FR-023**: System MUST carry forward prior-call context — at minimum the previous mood and at least one discussed topic or stated plan/intention — into the next call.
- **FR-024**: System MUST allow follow-up on a prior stated plan/intention on the subsequent call.
- **FR-025**: System MUST acknowledge important dates (e.g., birthdays/anniversaries) when a call falls on or near them (within ±1 day of the date).
- **FR-026**: All personal facts and call context classified as PHI MUST remain on PHI-compliant infrastructure and MUST NOT egress to non-covered services.

#### Scheduling (US5, US8)

- **FR-027**: System MUST support an independent evening call window per elder in addition to the morning window.
- **FR-028**: System MUST allow the evening window to be enabled or disabled independently of the morning window.
- **FR-029**: Each scheduled call MUST be placed only within its own local time window, honoring the elder's timezone and quiet-hours rules.
- **FR-030**: System MUST automatically place an outbound call for an accepted callback request at the requested time, subject to quiet-hours and DNC rules, deferring to the next allowed time when necessary.
- **FR-031**: All scheduling paths (morning, evening, callback auto-dial) MUST honor the DNC list and idempotency guarantees.

#### Wellbeing programs (US6)

- **FR-032**: System MUST administer a monthly structured wellbeing survey (loneliness, mood, satisfaction) and record structured results, no more than once per elder per month.
- **FR-033**: System MUST offer a mood-boosting activity (breathing, memory exercise, or light game) when the elder's mood is low — defined as a logged mood ≤ 2 on the 1–5 scale, or the elder expressing distress in conversation.
- **FR-034**: System MUST select activities so as not to repeat a recently-used activity until the catalog is exhausted — "recently-used" means used within the last 30 days OR among the last 3 activities for that elder, whichever is the larger exclusion set.
- **FR-035**: System MUST accept an elder's decline of a survey or activity gracefully and not push.

#### Safety education & opt-out (US7)

- **FR-036**: System MUST warn about likely scams and explain red flags when the elder describes a suspicious request.
- **FR-037**: System MUST add a number to the do-not-call list when the elder requests no more calls during a conversation.
- **FR-038**: System MUST add a number to the do-not-call list when an opt-out keyword (e.g., STOP) is received via inbound SMS.
- **FR-039**: System MUST notify the family contact and/or operator when an elder opts out, so the reason for stopping calls is understood.

#### Language (US8)

- **FR-040**: System MUST detect when an elder is speaking Spanish, promise a Spanish-language callback, record the language preference, and schedule a Spanish callback.

#### Informational SMS (cross-cutting)

- **FR-041**: System MUST be able to send the elder an informational SMS containing helpful information and relevant phone numbers (including emergency resource numbers) on request.

### Key Entities *(include if feature involves data)*

- **Family Contact**: A person linked to an elder who can send tasks and receive alerts/reports. Attributes: relationship to elder, phone number, alert preferences. Relationship: many family contacts per elder.
- **Family Task**: A short instruction from a family contact to be conveyed to the elder. Attributes: source contact, target elder, message, status (open → delivered/closed), safety-review flag.
- **Personal Fact**: A durable piece of knowledge about an elder. Attributes: category (person/routine/preference/important-date/health-context), content, source (operator-entered or elder-stated), recorded time, PHI classification.
- **Call Context / Conversation Summary**: A carry-forward record of what was discussed and any plans/intentions from a call, used to seed the next call.
- **Crisis Escalation**: An urgent record of a detected life-safety event. Attributes: category, severity, detection source (model / safety-net), resource offered, notification status.
- **Re-reminder**: A pending medication re-ask. Attributes: medication, elder, attempt count, status.
- **Wellbeing Survey Result**: Structured monthly survey outcome. Attributes: loneliness, mood, satisfaction, period.
- **Activity**: A catalog entry for a mood-boosting activity, plus per-elder recent-use tracking to avoid repetition.
- **Language Preference**: An elder's spoken-language preference (e.g., English/Spanish) driving callback language.
- **Family Report**: A generated monthly per-elder status-and-trends summary delivered to family.
- **Evening Call Window**: A second per-elder schedule window with independent enable/disable.

### Out of Scope (Deferred to Spec 003)

- Subscription / free-trial signup and management.
- Conversational FAQ answering about the service, safety, and wellbeing as a sales motion.
- Phone payment: DTMF card capture via Stripe and payment-link flows (PCI-scoped).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In a crisis test battery covering all five categories, 100% of explicit-crisis scripted calls produce the correct emergency resource, an urgent escalation, and a family alert — including cases where the conversational model alone would have missed the crisis (caught by the safety net).
- **SC-002**: False-escalation rate on the benign control set is at or below 2%.
- **SC-003**: Family-to-elder task relay completes the full loop (received → conveyed on next call → closed) for at least 95% of well-formed tasks, with zero duplicate deliveries of closed tasks.
- **SC-004**: Family alerts for missed calls and crises are delivered within 5 minutes of the triggering event for at least 99% of events.
- **SC-005**: For elders configured with an evening window, both morning and evening calls are placed on enabled days at least 98% of the time, and disabling the evening window suppresses the evening call 100% of the time.
- **SC-006**: Medication re-reminders are delivered for at least 95% of not-taken reports and stop at the configured cap with zero runaway nagging.
- **SC-007**: On the second of two consecutive calls, Clara references at least one prior-call fact or plan in at least 90% of evaluated call pairs.
- **SC-008**: The monthly wellbeing survey is administered exactly once per due elder per month (no duplicates, no misses on answered calls).
- **SC-009**: Mood-boosting activities offered on consecutive low-mood calls differ until the catalog is exhausted in 100% of evaluated sequences.
- **SC-010**: Opt-out requests (spoken or SMS STOP) result in no further outbound calls to that number 100% of the time.
- **SC-011**: Spanish-speaking callers receive a Spanish-callback promise and a scheduled Spanish callback in 100% of detected cases.
- **SC-012**: Monthly family reports are generated and delivered for 100% of elders with a registered family contact and at least one call in the period.
- **SC-013**: No PHI (personal facts, health context, transcripts, survey results) egresses to a non-BAA-covered service, verified by audit.

## Assumptions

- **Telephony/messaging substrate**: Inbound and outbound SMS use the existing Telnyx messaging integration; voice uses the existing Telnyx + LiveKit pipeline. No new telephony provider is introduced.
- **Family channel is SMS**: Family interaction (task intake, alerts, reports) is via SMS to/from Clara's number; no separate family app or portal is in scope for this spec.
- **Crisis safety net is layered, not replacing the model**: The deterministic layer is an additional safety net beneath the conversational model, not a replacement for it.
- **Spanish handling is callback-based**: Per the source requirement, when Spanish is detected Clara promises and schedules a Spanish callback rather than switching languages mid-call. Full bilingual in-call agents are out of scope here.
- **Personal facts reuse existing per-elder data model where possible**: Structured facts extend the existing contact data model rather than introducing a separate datastore, subject to the PHI-containment principle.
- **Callback auto-dial reuses existing scheduling/dialer**: The callback auto-dial path uses the existing outbound call, scheduling, quiet-hours, idempotency, and DNC machinery.
- **Monthly cadence is calendar-month** unless an operator configures otherwise.
- **Operator queue exists as the fallback recipient** for alerts/reports/tasks when no family contact is registered or a message is unmatched.
- **Activity and survey content** are platform-maintained catalogs (operator-curatable), not free-form per call.
- **Commercial layer (subscription, FAQ-as-sales, payment)** is explicitly deferred to spec 003 and is a prerequisite-independent follow-up.
- **Constitution constraints apply**: service isolation (`apps/api` ↔ `services/agent` via HTTPS only), PHI containment (Vertex AI for LLM, no PHI egress), idempotent outbound operations, audit logging, and test-first development govern all of the above.
