import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { agentConfigSchema, type AgentConfigForm } from "../../config/agentConfigSchema";
import { SECTION_LABELS, type SectionKey } from "../../config/fieldMeta";
import { Spinner } from "../../components/ui/spinner";
import { useIsAdmin } from "../../auth/useSession";
import { pushToast } from "../../components/ui/toast";
import type { ApiError } from "../../lib/api";
import type { AgentConfig } from "../../types/api";
import { useProfile, useSaveDraft } from "./hooks";
import { PublishDialog } from "./PublishDialog";
import { EditorToolbar } from "./EditorToolbar";
import { SectionRail } from "./SectionRail";
import { PromptsSection } from "./sections/PromptsSection";
import { VoiceSection } from "./sections/VoiceSection";
import { LLMSection } from "./sections/LLMSection";
import { STTSection } from "./sections/STTSection";
import { TimingSection } from "./sections/TimingSection";
import { ToolsSection } from "./sections/ToolsSection";
import { VoicemailSection } from "./sections/VoicemailSection";
import { SpeechAdvancedSection } from "./sections/SpeechAdvancedSection";

const SECTION_ORDER: SectionKey[] = [
  "prompts",
  "voice",
  "llm",
  "stt",
  "speech_advanced",
  "timing",
  "tools",
  "voicemail_detection",
];

// Validation-error shape FastAPI/Pydantic returns on 422. The api wrapper
// JSON-stringifies it into ApiError.detail, so we parse it back here.
interface ValidationItem {
  loc: (string | number)[];
  msg: string;
}

function tryParseFieldErrors(detail: string): ValidationItem[] | null {
  try {
    const parsed: unknown = JSON.parse(detail);
    if (Array.isArray(parsed)) {
      return parsed.filter(
        (i): i is ValidationItem =>
          typeof i === "object" && i !== null && "loc" in i && "msg" in i,
      );
    }
  } catch {
    return null;
  }
  return null;
}

export function ProfileEditorPage() {
  const { id = "" } = useParams();
  const isAdmin = useIsAdmin();
  const { data: profile, isLoading, isError, error } = useProfile(id);
  const saveDraft = useSaveDraft(id);

  const [section, setSection] = useState<SectionKey>("prompts");
  const [publishOpen, setPublishOpen] = useState(false);

  const form = useForm<AgentConfigForm>({
    resolver: zodResolver(agentConfigSchema),
    mode: "onBlur",
  });

  // Initialize the form once the profile draft_config is loaded. The server-side
  // AgentConfig types tools.enabled as string[]; the form schema narrows it to the
  // tool-name enum, so cast through the known-valid server payload.
  useEffect(() => {
    if (profile) form.reset(profile.draft_config as AgentConfigForm);
  }, [profile, form]);

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 p-8 text-slate-600">
        <Spinner /> Loading profile…
      </div>
    );
  }
  if (isError || !profile) {
    return (
      <p className="p-8 text-sm text-red-700">
        Failed to load profile: {(error as Error)?.message}
      </p>
    );
  }

  function mapServerErrors(detail: string): boolean {
    const items = tryParseFieldErrors(detail);
    if (!items || items.length === 0) return false;
    let mapped = false;
    for (const item of items) {
      // loc is like ["body", "config", "prompts", "greeting"]; drop the leading
      // "body"/"config" envelope to get the AgentConfig dotted path.
      const path = item.loc.filter((p) => p !== "body" && p !== "config").join(".");
      if (path) {
        form.setError(path as keyof AgentConfigForm, { type: "server", message: item.msg });
        mapped = true;
      }
    }
    return mapped;
  }

  const onSave = form.handleSubmit((values: AgentConfigForm) => {
    saveDraft.mutate(
      { config: values as AgentConfig },
      {
        onError: (err: ApiError) => {
          if (err.status === 422 && mapServerErrors(err.detail)) {
            pushToast("Some fields were rejected by the server — see the highlighted errors.");
          } else {
            pushToast(err.detail);
          }
        },
        onSuccess: () => pushToast("Draft saved.", "info"),
      },
    );
  });

  async function onPublishClick(): Promise<void> {
    // Validate before opening the diff so the live-vs-draft comparison reflects a
    // config the server will accept.
    const valid = await form.trigger();
    if (!valid) {
      pushToast("Fix validation errors before publishing.");
      return;
    }
    // Publish freezes the SAVED draft_config server-side (it takes no config body),
    // so persist any unsaved edits first. Otherwise the diff (form values) would
    // misrepresent what goes live and unsaved changes would be silently dropped.
    if (form.formState.isDirty) {
      try {
        await saveDraft.mutateAsync({ config: form.getValues() as AgentConfig });
      } catch (err) {
        const e = err as ApiError;
        if (e.status === 422 && mapServerErrors(e.detail)) {
          pushToast("Some fields were rejected by the server — see the highlighted errors.");
        } else {
          pushToast(e.detail);
        }
        return;
      }
    }
    setPublishOpen(true);
  }

  const draftValues = form.watch();
  const summaries: Partial<Record<SectionKey, string>> = {
    llm: draftValues.llm?.model,
    voice: draftValues.voice?.cartesia_voice_id ?? "default",
    tools: `${draftValues.tools?.enabled?.length ?? 0} on`,
    timing: draftValues.timing ? `${draftValues.timing.answer_timeout_s}s` : undefined,
  };

  return (
    <div className="flex h-full flex-col">
      <EditorToolbar
        name={profile.name}
        status={profile.status}
        publishedVersion={profile.published_version}
        dirty={form.formState.isDirty}
        model={draftValues.llm?.model ?? "—"}
        voice={draftValues.voice?.cartesia_voice_id ?? "default"}
        language={draftValues.voice?.language ?? "default"}
        isAdmin={isAdmin}
        saving={saveDraft.isPending}
        profileId={id}
        onJump={(s) => setSection(s)}
        onSave={onSave}
        onPublish={onPublishClick}
      />
      <div className="flex min-h-0 flex-1">
        <div className="min-w-0 flex-1 overflow-y-auto px-8 py-6">
          <div className="mx-auto max-w-3xl">
            <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500">
              {SECTION_LABELS[section]}
            </h2>
            <form className="min-w-0" onSubmit={onSave}>
              <fieldset disabled={!isAdmin} className="min-w-0">
                {section === "prompts" ? <PromptsSection form={form} /> : null}
                {section === "voice" ? <VoiceSection form={form} /> : null}
                {section === "llm" ? <LLMSection form={form} /> : null}
                {section === "stt" ? <STTSection form={form} /> : null}
                {section === "speech_advanced" ? <SpeechAdvancedSection form={form} /> : null}
                {section === "timing" ? <TimingSection form={form} /> : null}
                {section === "tools" ? <ToolsSection form={form} /> : null}
                {section === "voicemail_detection" ? <VoicemailSection form={form} /> : null}
              </fieldset>
            </form>
          </div>
        </div>
        <aside className="w-64 shrink-0 overflow-y-auto border-l border-slate-200 bg-white px-3 py-4">
          <SectionRail
            order={SECTION_ORDER}
            active={section}
            summaries={summaries}
            onSelect={(s) => setSection(s)}
          />
        </aside>
      </div>

      <PublishDialog
        open={publishOpen}
        onClose={() => setPublishOpen(false)}
        profileId={id}
        draftConfig={draftValues as AgentConfig}
        publishedVersion={profile.published_version}
        onPublished={() => {
          setPublishOpen(false);
          pushToast("Published.", "info");
        }}
      />
    </div>
  );
}
