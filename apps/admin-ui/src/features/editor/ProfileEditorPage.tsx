import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { agentConfigSchema, type AgentConfigForm } from "../../config/agentConfigSchema";
import { SECTION_LABELS, type SectionKey } from "../../config/fieldMeta";
import { Tabs } from "../../components/ui/tabs";
import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";
import { Spinner } from "../../components/ui/spinner";
import { useIsAdmin } from "../../auth/useSession";
import { pushToast } from "../../components/ui/toast";
import type { ApiError } from "../../lib/api";
import type { AgentConfig } from "../../types/api";
import { useProfile, useSaveDraft } from "./hooks";
import { PublishDialog } from "./PublishDialog";
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
      <div className="flex items-center gap-2 text-gray-600">
        <Spinner /> Loading profile…
      </div>
    );
  }
  if (isError || !profile) {
    return (
      <p className="text-sm text-red-700">Failed to load profile: {(error as Error)?.message}</p>
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
    setPublishOpen(true);
  }

  const tabItems = SECTION_ORDER.map((k) => ({ key: k, label: SECTION_LABELS[k] }));
  const draftValues = form.watch();

  return (
    <div className="space-y-4">
      <div className="sticky top-0 z-10 -mx-6 -mt-6 mb-2 border-b border-gray-200 bg-white px-6 py-3">
        <div className="flex items-center justify-between">
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-semibold">{profile.name}</h1>
              <Badge tone={profile.status === "active" ? "green" : "gray"}>{profile.status}</Badge>
              {profile.published_version !== null ? (
                <Badge tone="blue">live v{profile.published_version}</Badge>
              ) : (
                <Badge tone="gray">unpublished</Badge>
              )}
              {form.formState.isDirty ? <Badge tone="amber">unsaved changes</Badge> : null}
            </div>
            <Link to={`/profiles/${id}/versions`} className="text-xs text-blue-600 hover:underline">
              Version history
            </Link>
          </div>
          {isAdmin ? (
            <div className="flex gap-2">
              <Button variant="secondary" onClick={onSave} disabled={saveDraft.isPending}>
                {saveDraft.isPending ? "Saving…" : "Save draft"}
              </Button>
              <Button onClick={onPublishClick}>Publish</Button>
            </div>
          ) : (
            <span className="text-xs text-gray-500">Read-only (viewer role)</span>
          )}
        </div>
      </div>

      <div className="flex gap-6">
        <div className="w-44 shrink-0">
          <Tabs items={tabItems} active={section} onSelect={(k) => setSection(k as SectionKey)} />
        </div>
        <form className="min-w-0 flex-1" onSubmit={onSave}>
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
