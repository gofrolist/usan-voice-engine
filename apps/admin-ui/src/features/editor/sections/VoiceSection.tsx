import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { UseFormReturn } from "react-hook-form";
import { Controller } from "react-hook-form";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { useVoiceCatalog, type VoiceSpec } from "../../../config/voiceCatalog";
import { Input } from "../../../components/ui/input";
import { cn } from "../../../lib/cn";
import { Field } from "./Field";
import { NumberControl, TextControl } from "./controls";

// Inline SVGs keep the compact picker self-contained (no icon dependency). All are
// aria-hidden: the play button carries its own aria-label, and selection state is
// announced via aria-pressed on the row, so the check mark is purely decorative.
function PlayIcon() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" className="h-3 w-3 fill-current">
      <path d="M5 3.5 13 8 5 12.5Z" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" className="h-3 w-3 fill-current">
      <rect x="4" y="4" width="8" height="8" rx="1.5" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      aria-hidden="true"
      className="h-4 w-4 fill-none stroke-indigo-600"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="m3.5 8.5 3 3 6-7" />
    </svg>
  );
}

// Display labels for the voice gender enum (avoid surfacing the raw "gender_neutral" token).
const GENDER_LABEL: Record<NonNullable<VoiceSpec["gender"]>, string> = {
  masculine: "masculine",
  feminine: "feminine",
  gender_neutral: "neutral",
};

// US2 / FR-009, FR-010: a searchable, curated voice picker with a per-voice audio
// preview. The selected value remains AgentConfig.voice.cartesia_voice_id (a plain
// string — never a Zod enum), so a published config referencing a withdrawn voice still
// loads. The picker offers only ACTIVE catalog voices for new selection but renders a
// deprecation marker when the currently-selected voice is deprecated or no longer in the
// catalog. tts_model / speed / language remain free knobs below the picker.

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

  const audioRef = useRef<HTMLAudioElement | null>(null);

  // Stop any in-flight sample and free its blob URL. Called before starting a new
  // sample and on unmount, so navigating away never leaves audio playing or leaks a
  // blob URL (the previous fire-and-forget `new Audio()` did both).
  const stopSample = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    // Drop the ref first: any media event that fires on this element after we
    // stop it is now stale and must be ignored by onPlay's finish() guard.
    audioRef.current = null;
    audio.pause();
    if (audio.src.startsWith("blob:")) URL.revokeObjectURL(audio.src);
    // removeAttribute, NOT `audio.src = ""`: an empty src resolves to the page
    // URL, which the element tries to load and then fires a spurious "error"
    // event — surfacing a bogus "Could not play this voice sample" on every
    // stop/switch even though playback was fine.
    audio.removeAttribute("src");
  }, []);

  useEffect(() => stopSample, [stopSample]);

  // Fetch the fixed PHI-free sample from the server proxy (raw fetch, not the JSON
  // `api` wrapper, because it streams audio/mpeg) and play it. The Cartesia secret
  // stays server-side; the browser only ever sees the audio blob.
  async function onPlay(voiceId: string): Promise<void> {
    setPlayError(null);
    stopSample();
    setPlaying(voiceId);
    let audio: HTMLAudioElement | null = null;
    try {
      const res = await fetch(`/v1/admin/voice-catalog/${encodeURIComponent(voiceId)}/sample`, {
        credentials: "include",
      });
      if (!res.ok) throw new Error("voice sample unavailable");
      const blob = await res.blob();
      const el = new Audio(URL.createObjectURL(blob));
      audio = el;
      audioRef.current = el;
      const finish = (failed: boolean): void => {
        // Only the active element drives UI state. An error/ended event from an
        // element we already stopped or replaced (audioRef has moved on) is
        // stale and must not surface an error or clear a newer selection.
        if (audioRef.current !== el) return;
        if (el.src.startsWith("blob:")) URL.revokeObjectURL(el.src);
        audioRef.current = null;
        setPlaying((p) => (p === voiceId ? null : p));
        if (failed) setPlayError("Could not play this voice sample. Please try again.");
      };
      el.addEventListener("ended", () => finish(false));
      el.addEventListener("error", () => finish(true));
      await el.play();
    } catch {
      // A play() rejection caused by us stopping/replacing this element (e.g.
      // AbortError) is expected — only report when this element is still active,
      // or when we failed before creating one (a real fetch/network error).
      if (audio && audioRef.current !== audio) return;
      stopSample();
      setPlaying((p) => (p === voiceId ? null : p));
      setPlayError("Could not play this voice sample. Please try again.");
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
          <ul className="space-y-1.5">
            {filtered.map((v) => {
              const isSelected = field.value === v.cartesia_voice_id;
              const isPlaying = playing === v.cartesia_voice_id;
              return (
                <li
                  key={v.cartesia_voice_id}
                  className={cn(
                    "flex items-center gap-3 rounded-lg border px-3 py-2 transition-colors",
                    isSelected
                      ? "border-indigo-400 bg-indigo-50"
                      : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50",
                  )}
                >
                  {/* Compact play control (Cartesia-style ▶ circle), a separate sibling
                      button so the rest of the row stays a single select target. */}
                  <button
                    type="button"
                    className={cn(
                      "flex h-8 w-8 shrink-0 items-center justify-center rounded-full border transition-colors",
                      isPlaying
                        ? "border-indigo-300 bg-indigo-100 text-indigo-700"
                        : "border-slate-300 text-slate-600 hover:bg-slate-100",
                    )}
                    aria-label={isPlaying ? `Stop sample of ${v.name}` : `Play sample of ${v.name}`}
                    onClick={() => {
                      if (isPlaying) {
                        stopSample();
                        setPlaying(null);
                      } else {
                        void onPlay(v.cartesia_voice_id);
                      }
                    }}
                  >
                    {isPlaying ? <StopIcon /> : <PlayIcon />}
                  </button>
                  {/* The select target fills the rest of the row, so clicking anywhere to
                      the right of the play button selects the voice (not just the text). */}
                  <button
                    type="button"
                    className="flex min-w-0 flex-1 items-center justify-between gap-3 text-left"
                    aria-pressed={isSelected}
                    onClick={() => field.onChange(v.cartesia_voice_id)}
                  >
                    <span className="min-w-0">
                      <span className="block truncate text-sm font-medium text-slate-900">
                        {v.name}
                      </span>
                      <span className="block truncate text-xs text-slate-500">{v.description}</span>
                    </span>
                    <span className="flex shrink-0 items-center gap-2">
                      <span className="text-xs text-slate-400">
                        {v.language}
                        {v.gender ? ` · ${GENDER_LABEL[v.gender]}` : ""}
                      </span>
                      {isSelected ? <CheckIcon /> : null}
                    </span>
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
