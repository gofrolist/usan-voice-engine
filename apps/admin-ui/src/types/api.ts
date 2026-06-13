// Hand-written TypeScript mirrors of the apps/api Pydantic schemas.
// Nullability and value sets MUST match the server exactly:
//   - schemas/agent_config.py (AgentConfig + 8 sub-configs)
//   - schemas/agent_profile.py (Profile/Version summaries + details + requests)
//   - schemas/admin.py (AuditEntryOut, ElderSummary, AssignProfileRequest)
//   - schemas/auth.py (MeResponse, AdminUserOut, AdminUserCreate)
//   - schemas/admin_calls.py (AdminCallSummary, AdminCallDetail + CallOrigin,
//     TranscriptSegment from schemas/call.py)
//   - schemas/admin_tools.py (FollowupFlagSummary, CallbackRequestSummary, QueuesSummary)

// ---------------------------------------------------------------------------
// Agent config (agent_config.py)
// ---------------------------------------------------------------------------

export interface PromptsConfig {
  system_prompt: string;
  greeting: string;
  recording_disclosure: string;
  voicemail_message: string;
  checkin_flow_instructions: string;
  goodbye_message: string;
  inbound_opening: string;
  inbound_personalization_template: string;
}

export interface VoiceConfig {
  cartesia_voice_id: string | null;
  tts_model: string | null;
  speed: number | null;
  language: string | null;
}

export interface LLMConfig {
  model: string;
  temperature: number | null;
}

export interface STTConfig {
  model: string;
  language: string | null;
}

export interface TimingConfig {
  answer_timeout_s: number;
  max_call_duration_s: number;
}

export interface ToolsConfig {
  enabled: string[];
}

export interface VoicemailDetectionConfig {
  window_s: number;
  trigger_phrases: string[];
}

export type TurnDetection = "english" | "multilingual" | "vad";

export interface SpeechAdvancedConfig {
  vad_min_silence_s: number | null;
  vad_activation_threshold: number | null;
  turn_detection: TurnDetection | null;
  min_endpointing_delay_s: number | null;
  max_endpointing_delay_s: number | null;
  min_interruption_duration_s: number | null;
  min_interruption_words: number | null;
}

// Per-status retry caps in chain-global attempt semantics (RetryMaxAttempts).
export interface RetryMaxAttempts {
  no_answer: number | null;
  voicemail_left: number | null;
  busy: number | null;
  failed: number | null;
}

// Per-profile policy overrides (PolicyConfig). Quiet-hours values are "HH:MM"
// strings narrowing within the statutory 09:00–21:00; enforced server-side.
export interface PolicyConfig {
  quiet_hours_start_local: string | null;
  quiet_hours_end_local: string | null;
  retry_delay_multiplier: number | null;
  retry_max_attempts: RetryMaxAttempts | null;
}

export interface AgentConfig {
  prompts: PromptsConfig;
  voice: VoiceConfig;
  llm: LLMConfig;
  stt: STTConfig;
  timing: TimingConfig;
  tools: ToolsConfig;
  voicemail_detection: VoicemailDetectionConfig;
  speech_advanced: SpeechAdvancedConfig;
  // Optional-with-default on the server (forward compat): older snapshots and
  // drafts lack the key entirely.
  policy?: PolicyConfig | null;
}

// ---------------------------------------------------------------------------
// Profiles & versions (agent_profile.py)
// ---------------------------------------------------------------------------

export type ProfileStatus = "active" | "archived";

export interface ProfileSummary {
  id: string;
  name: string;
  description: string | null;
  status: ProfileStatus;
  is_default_inbound: boolean;
  is_default_outbound: boolean;
  published_version: number | null;
  has_unpublished_draft: boolean;
  assigned_elder_count: number;
  // Optimistic-concurrency token (FR-032); see ProfileDetail.draft_revision.
  draft_revision: number;
  updated_at: string;
}

export interface ProfileDetail {
  id: string;
  name: string;
  description: string | null;
  status: ProfileStatus;
  is_default_inbound: boolean;
  is_default_outbound: boolean;
  published_version: number | null;
  draft_config: AgentConfig;
  // Optimistic-concurrency token (FR-032): loaded with the draft, echoed back as
  // DraftUpdate.expected_revision on save; a stale value yields HTTP 409.
  draft_revision: number;
  created_by: string | null;
  updated_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface VersionSummary {
  version: number;
  note: string | null;
  published_by: string | null;
  published_at: string;
}

export interface VersionDetail extends VersionSummary {
  config: AgentConfig;
}

export type Direction = "inbound" | "outbound";

export interface ProfileCreate {
  name: string;
  description?: string | null;
  clone_from?: string | null;
}

export interface DraftUpdate {
  config: AgentConfig;
  description?: string | null;
  // Optimistic concurrency (FR-032): the draft_revision the editor loaded. Omitted
  // -> unconditional save (backward compatible); the editor always sends it.
  expected_revision?: number;
}

export interface PublishRequest {
  note?: string | null;
}

export interface SetDefaultRequest {
  direction: Direction;
}

// ---------------------------------------------------------------------------
// Defaults area read model (schemas/admin_defaults.py — US3 / FR-016..020)
// ---------------------------------------------------------------------------

// Why a flagged default is no longer effective (FR-020). null when it resolves.
export type IneligibleReason = "archived" | "unpublished";

// The profile currently flagged default for a direction (name/id only — no PHI).
export interface DefaultProfileRef {
  id: string;
  name: string;
  status: ProfileStatus;
  published_version: number | null;
  // True iff this default actually resolves for a call (active + published).
  eligible: boolean;
}

export interface DirectionDefault {
  direction: Direction;
  // null when no profile is flagged default for this direction.
  default_profile: DefaultProfileRef | null;
  // True when a default IS flagged but no longer effective (archived/unpublished).
  ineligible: boolean;
  ineligible_reason: IneligibleReason | null;
}

export interface DefaultsView {
  directions: DirectionDefault[];
  // Plain-language resolution order, highest precedence first (FR-017).
  resolution_order: string[];
  // The built-in last-resort fallback config, read-only (FR-017/FR-019).
  builtin_fallback: AgentConfig;
}

// ---------------------------------------------------------------------------
// Admin (admin.py)
// ---------------------------------------------------------------------------

export interface AuditEntry {
  id: number;
  actor_email: string;
  action: string;
  entity_type: string | null;
  entity_id: string | null;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface ElderSummary {
  id: string;
  name: string;
  masked_phone: string;
  agent_profile_id: string | null;
  agent_profile_name: string | null;
}

export interface AssignProfileRequest {
  agent_profile_id: string | null;
}

// ---------------------------------------------------------------------------
// Auth (auth.py)
// ---------------------------------------------------------------------------

export type AdminUserRole = "admin" | "viewer";

export interface Me {
  email: string;
  role: AdminUserRole;
}

export interface AdminUser {
  email: string;
  role: AdminUserRole;
  added_by: string | null;
}

export interface AdminUserCreate {
  email: string;
  role: AdminUserRole;
}

// ---------------------------------------------------------------------------
// Calls console (admin_calls.py + call.py)
// ---------------------------------------------------------------------------

export interface CallOrigin {
  source: "schedule" | "batch";
  id: string;
  ordinal: string | number; // local_date for schedules, target_index for batches
}

export interface TranscriptSegment {
  role: string;
  content: string;
  tool_name: string | null;
  tool_args: Record<string, unknown> | null;
  started_at: string;
  ended_at: string | null;
}

export interface AdminCallSummary {
  id: string;
  elder_id: string | null;
  elder_name: string | null;
  masked_phone: string; // "***" + last 4 — the raw phone never reaches this plane
  direction: string;
  status: string;
  origin: CallOrigin | null;
  attempt: number;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  end_reason: string | null;
  has_recording: boolean;
  created_at: string;
}

export interface AdminCallDetail extends AdminCallSummary {
  livekit_room: string | null;
  parent_call_id: string | null;
  scheduled_at: string | null;
  answered_at: string | null;
  recording_status: string | null;
  presigned_recording_url: string | null;
  recording_url_ttl_s: number | null;
  transcript: TranscriptSegment[];
}

// ---------------------------------------------------------------------------
// Ops queues (admin_tools.py)
// ---------------------------------------------------------------------------

export type QueueStatus = "open" | "acknowledged" | "resolved";

export interface FollowupFlagSummary {
  id: number;
  call_id: string;
  elder_id: string;
  elder_name: string | null;
  masked_phone: string;
  severity: string;
  category: string;
  reason: string | null;
  status: QueueStatus;
  status_updated_at: string | null;
  status_updated_by: string | null;
  created_at: string;
}

export interface CallbackRequestSummary {
  id: number;
  call_id: string;
  elder_id: string;
  elder_name: string | null;
  masked_phone: string;
  requested_time_text: string;
  requested_at: string | null;
  notes: string | null;
  status: QueueStatus;
  status_updated_at: string | null;
  status_updated_by: string | null;
  created_at: string;
}

export interface QueuesSummary {
  flags_open: number;
  flags_open_urgent: number;
  flags_acknowledged: number;
  callbacks_open: number;
  callbacks_acknowledged: number;
}
