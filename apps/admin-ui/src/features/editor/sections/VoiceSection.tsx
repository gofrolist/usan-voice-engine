import { useMemo, useState } from "react";
import type { UseFormReturn } from "react-hook-form";
import { Controller } from "react-hook-form";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { useVoiceCatalog, type VoiceSpec } from "../../../config/voiceCatalog";
import { Input } from "../../../components/ui/input";
import { Field } from "./Field";
import { NumberControl, TextControl } from "./controls";

// US2 / FR-009, FR-010: a searchable, curated voice picker with a per-voice audio
// preview. The selected value remains AgentConfig.voice.cartesia_voice_id (a plain
// string — never a Zod enum), so a published config referencing a withdrawn voice still
// loads. The picker offers only ACTIVE catalog voices for new selection but renders a
// deprecation marker when the currently-selected voice is deprecated or no longer in the
// catalog. tts_model / speed / language remain free knobs below the picker.

// Fetch the fixed PHI-free sample for a voice from the server proxy and play it. Uses
// fetch (not the JSON `api` wrapper) because the endpoint streams audio/mpeg bytes. The
// secret stays server-side; the browser only ever sees the audio blob.
async function playVoiceSample(voiceId: string): Promise<void> {
  const res = await fetch(`/v1/admin/voice-catalog/${encodeURIComponent(voiceId)}/sample`, {
    credentials: "include",
  });
  if (!res.ok) {
    throw new Error("voice sample unavailable");
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  audio.addEventListener("ended", () => URL.revokeObjectURL(url));
  await audio.play();
}

function VoicePicker({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const { data: voices, isError, isLoading } = useVoiceCatalog();
  const [query, setQuery] = useState("");
  const [playing, setPlaying] = useState<string | null>(null);
  const [playError, setPlayError] = useState<string | null>(null);

  const selected = form.watch("voice.cartesia_voice_id");
  const byId = useMemo(
    () => new Map<string, VoiceSpec>((voices ?? []).map((v) => [v.cartesia_voice_id, v])),
    [voices],
  );

  // Active (non-deprecated) catalog voices, filtered by the free-text search across
  // name/description/language/gender so an operator can find a voice without typing ids.
  const active = (voices ?? []).filter((v) => !v.deprecated);
  const q = query.trim().toLowerCase();
  const filtered = q
    ? active.filter((v) =>
        [v.name, v.description, v.language, v.gender ?? ""]
          .join(" ")
          .toLowerCase()
          .includes(q),
      )
    : active;

  // The selected value may be a withdrawn id (a published config) — show a deprecation
  // marker so the operator understands why it is not in the active list (FR-010).
  const selectedSpec = selected ? byId.get(selected) : undefined;
  const selectedIsWithdrawn = Boolean(selected) && (selectedSpec?.deprecated ?? !selectedSpec);

  async function onPlay(voiceId: string): Promise<void> {
    setPlayError(null);
    setPlaying(voiceId);
    try {
      await playVoiceSample(voiceId);
    } catch {
      setPlayError("Could not play this voice sample. Please try again.");
    } finally {
      setPlaying(null);
    }
  }

  return (
    <Controller
      control={form.control}
      name="voice.cartesia_voice_id"
      render={({ field }) => (
        <div className="space-y-3">
          <label className="block text-sm font-medium text-slate-700" htmlFor="voice-search">
            Voice
          </label>
          <p className="text-xs text-slate-500">
            Pick a voice from the curated catalog, or leave it on the plugin default. Use Play
            sample to hear how each one sounds.
          </p>
          {isError ? (
            <p className="text-xs font-medium text-red-700">
              Could not load voice catalog — please refresh.
            </p>
          ) : null}
          {selectedIsWithdrawn ? (
            <p className="text-xs font-medium text-amber-700">
              The selected voice (<span className="font-mono">{selected}</span>) is deprecated or no
              longer offered. Published calls keep using it until you pick a current voice.
            </p>
          ) : null}
          <Input
            id="voice-search"
            placeholder="Search voices (name, style, language)…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <div className="flex items-center justify-between">
            <button
              type="button"
              className="text-xs font-medium text-indigo-700 hover:underline disabled:text-slate-400"
              disabled={field.value === null}
              onClick={() => field.onChange(null)}
            >
              Use plugin default (clear)
            </button>
            {field.value === null ? (
              <span className="text-xs text-slate-500">Using plugin default</span>
            ) : null}
          </div>
          {playError ? <p className="text-xs font-medium text-red-700">{playError}</p> : null}
          <ul className="space-y-2">
            {filtered.map((v) => {
              const isSelected = field.value === v.cartesia_voice_id;
              return (
                <li
                  key={v.cartesia_voice_id}
                  className={`flex items-start justify-between gap-3 rounded-xl border px-4 py-3 shadow-card ${
                    isSelected ? "border-indigo-400 bg-indigo-50" : "border-slate-200 bg-white"
                  }`}
                >
                  <button
                    type="button"
                    className="min-w-0 text-left"
                    aria-pressed={isSelected}
                    onClick={() => field.onChange(v.cartesia_voice_id)}
                  >
                    <span className="block text-sm font-medium text-slate-900">{v.name}</span>
                    <span className="mt-0.5 block text-xs text-slate-500">{v.description}</span>
                    <span className="mt-0.5 block text-xs text-slate-400">
                      {v.language}
                      {v.gender ? ` · ${v.gender}` : ""}
                    </span>
                  </button>
                  <button
                    type="button"
                    className="shrink-0 rounded-lg border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
                    aria-label={`Play sample of ${v.name}`}
                    disabled={playing === v.cartesia_voice_id}
                    onClick={() => void onPlay(v.cartesia_voice_id)}
                  >
                    {playing === v.cartesia_voice_id ? "Playing…" : "Play sample"}
                  </button>
                </li>
              );
            })}
          </ul>
          {isLoading ? <p className="text-xs text-slate-400">Loading voice catalog…</p> : null}
          {!isLoading && !isError && filtered.length === 0 ? (
            <p className="text-xs text-slate-500">No voices match your search.</p>
          ) : null}
        </div>
      )}
    />
  );
}

export function VoiceSection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const errors = form.formState.errors.voice;
  return (
    <div className="space-y-5">
      <VoicePicker form={form} />
      <Field path="voice.tts_model" error={errors?.tts_model?.message}>
        <TextControl
          control={form.control}
          name="voice.tts_model"
          id="voice.tts_model"
          nullable
          placeholder="(plugin default)"
        />
      </Field>
      <Field path="voice.speed" error={errors?.speed?.message}>
        <NumberControl
          control={form.control}
          name="voice.speed"
          id="voice.speed"
          nullable
          step="0.05"
          min={0.25}
          max={4.0}
        />
      </Field>
      <Field path="voice.language" error={errors?.language?.message}>
        <TextControl
          control={form.control}
          name="voice.language"
          id="voice.language"
          nullable
          placeholder="(plugin default)"
        />
      </Field>
    </div>
  );
}
