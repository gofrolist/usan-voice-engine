import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";

// Mirrors apps/api/src/usan_api/schemas/voice_catalog.py (VoiceSpec). The API is
// authoritative; the frontend fetches the catalog at runtime so the VoiceSection picker
// never hand-duplicates the inventory. Unlike the tool catalog, an out-of-catalog voice
// is NOT a hard client error — the Zod schema keeps cartesia_voice_id a plain string
// (forward-compat: a published config may reference a now-deprecated/withdrawn id). The
// server 422 is the authoritative save-time gate (FR-014); the picker only offers the
// active catalog and surfaces a deprecation marker for a withdrawn selected value.
export interface VoiceSpec {
  cartesia_voice_id: string;
  name: string;
  language: string;
  // Optional metadata for the picker's language/gender filtering.
  gender: "masculine" | "feminine" | "gender_neutral" | null;
  description: string;
  tts_model_hint: string | null;
  // Hidden from NEW selection; a published config referencing it still loads with a
  // deprecation marker (FR-010 deprecation UX).
  deprecated: boolean;
}

interface VoiceCatalogResponse {
  voices: VoiceSpec[];
}

// Catalog is a global constant on the server (not per-version), so it is highly
// cacheable. Long staleTime avoids refetching it on every editor mount (mirrors
// toolCatalog.ts).
const CATALOG_KEY = ["voice-catalog"] as const;

export function useVoiceCatalog() {
  return useQuery<VoiceSpec[]>({
    queryKey: CATALOG_KEY,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const res = await api.get<VoiceCatalogResponse>("/v1/admin/voice-catalog");
      return res.voices;
    },
  });
}
