// Hand-written TypeScript mirrors of the apps/api Pydantic schemas.
// Nullability and value sets MUST match the server exactly:
//   - schemas/agent_config.py (AgentConfig + 8 sub-configs)
//   - schemas/agent_profile.py (Profile/Version summaries + details + requests)
//   - schemas/admin.py (AuditEntryOut, ElderSummary, AssignProfileRequest)
//   - schemas/auth.py (MeResponse, AdminUserOut, AdminUserCreate)

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

export interface AgentConfig {
  prompts: PromptsConfig;
  voice: VoiceConfig;
  llm: LLMConfig;
  stt: STTConfig;
  timing: TimingConfig;
  tools: ToolsConfig;
  voicemail_detection: VoicemailDetectionConfig;
  speech_advanced: SpeechAdvancedConfig;
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
}

export interface PublishRequest {
  note?: string | null;
}

export interface SetDefaultRequest {
  direction: Direction;
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
