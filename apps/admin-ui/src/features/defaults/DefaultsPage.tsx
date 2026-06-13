import { Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Select } from "../../components/ui/select";
import { Spinner } from "../../components/ui/spinner";
import { Badge } from "../../components/ui/badge";
import { useIsAdmin } from "../../auth/useSession";
import { useProfiles, useSetDefault } from "../profiles/hooks";
import { DEFAULTS_KEY, useDefaults } from "./hooks";
import type { Direction, DirectionDefault, ProfileSummary, AgentConfig } from "../../types/api";

const DIRECTION_HELP: Record<Direction, string> = {
  inbound: "Used for contact-initiated calls with no per-contact assignment.",
  outbound: "Used for scheduled wellness calls with no per-contact assignment.",
};

const DIRECTION_LABEL: Record<Direction, string> = {
  inbound: "Inbound default",
  outbound: "Outbound default",
};

const INELIGIBLE_HELP: Record<"archived" | "unpublished", string> = {
  archived: "This default profile was archived, so it is no longer effective.",
  unpublished: "This default profile has no published version, so it is no longer effective.",
};

interface DirectionCardProps {
  state: DirectionDefault;
  eligible: ProfileSummary[];
  canEdit: boolean;
  pending: boolean;
  onSelect: (direction: Direction, id: string) => void;
}

function DirectionCard({ state, eligible, canEdit, pending, onSelect }: DirectionCardProps) {
  const { direction, default_profile: current, ineligible, ineligible_reason: reason } = state;
  const effective = current !== null && current.eligible;
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-card">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold">{DIRECTION_LABEL[direction]}</h2>
        {current ? (
          <Badge tone={effective ? "blue" : "amber"}>{current.name}</Badge>
        ) : (
          <Badge tone="gray">no default set</Badge>
        )}
      </div>
      <p className="mt-1 text-xs text-slate-500">{DIRECTION_HELP[direction]}</p>

      {/* What actually runs today, in plain language (FR-016). */}
      <p className="mt-2 text-sm text-slate-700">
        {effective && current ? (
          <>
            Runs the published configuration of <span className="font-medium">{current.name}</span>.
          </>
        ) : (
          <>No effective default — calls fall back to the built-in configuration below.</>
        )}
      </p>

      {/* Ineligible-default warning + replacement prompt (FR-020). */}
      {ineligible ? (
        <div className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
          <p className="font-medium">This default is no longer effective.</p>
          <p className="mt-1 text-xs">
            {reason ? INELIGIBLE_HELP[reason] : "This default is no longer effective."} Choose a
            published replacement below so unassigned calls do not fall through to the built-in
            fallback.
          </p>
        </div>
      ) : null}

      {effective && current ? (
        <p className="mt-3">
          <Link
            to={`/profiles/${current.id}`}
            className="text-sm font-medium text-blue-700 hover:underline"
          >
            Edit this default profile
          </Link>
        </p>
      ) : null}

      {canEdit ? (
        <div className="mt-3 max-w-sm">
          <Select
            aria-label={`Default ${direction} profile`}
            value=""
            disabled={pending}
            onChange={(e) => {
              if (e.target.value !== "") onSelect(direction, e.target.value);
            }}
          >
            <option value="" disabled>
              {ineligible || !current ? "Choose a profile…" : "Change default…"}
            </option>
            {eligible.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </Select>
        </div>
      ) : null}
    </div>
  );
}

interface FallbackRowProps {
  label: string;
  value: string;
}

function FallbackRow({ label, value }: FallbackRowProps) {
  return (
    <div className="flex justify-between gap-4 py-1 text-sm">
      <span className="text-slate-500">{label}</span>
      <span className="font-mono text-slate-800">{value}</span>
    </div>
  );
}

function BuiltinFallbackPanel({ config }: { config: AgentConfig }) {
  return (
    <div
      data-testid="builtin-fallback"
      className="rounded-xl border border-slate-200 bg-slate-50 p-4"
    >
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold">Built-in fallback</h2>
        <Badge tone="gray">read-only</Badge>
      </div>
      <p className="mt-1 text-xs text-slate-500">
        The last-resort configuration used when no override, contact assignment, or per-direction
        default applies. This baseline is not editable.
      </p>
      <div className="mt-3 divide-y divide-slate-200 border-t border-slate-200">
        <FallbackRow label="Voice" value={config.voice.cartesia_voice_id ?? "plugin default"} />
        <FallbackRow label="LLM model" value={config.llm.model} />
        <FallbackRow label="Speech-to-text model" value={config.stt.model} />
        <FallbackRow label="Greeting" value={config.prompts.greeting} />
      </div>
    </div>
  );
}

export function DefaultsPage() {
  const isAdmin = useIsAdmin();
  const qc = useQueryClient();
  const { data: view, isLoading, isError, error } = useDefaults();
  const { data: profiles } = useProfiles();
  const setDefault = useSetDefault();

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 text-slate-600">
        <Spinner /> Loading…
      </div>
    );
  }
  if (isError || !view) {
    return (
      <p className="text-sm text-red-700">Failed to load defaults: {(error as Error)?.message}</p>
    );
  }

  // Eligible replacement candidates (active + published). The server already
  // computes effectiveness; this only constrains what the replacement select offers.
  const eligible = (profiles ?? []).filter(
    (p) => p.status === "active" && p.published_version !== null,
  );

  function change(direction: Direction, id: string): void {
    setDefault.mutate(
      { id, direction },
      { onSuccess: () => void qc.invalidateQueries({ queryKey: DEFAULTS_KEY }) },
    );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Default profiles</h1>
      {!isAdmin ? (
        <p className="text-sm text-slate-500">
          Read-only (viewer role). Only admins can change defaults.
        </p>
      ) : null}

      {/* Plain-language resolution order (FR-017), highest precedence first. */}
      <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-card">
        <h2 className="text-sm font-semibold">Resolution order</h2>
        <p className="mt-1 text-xs text-slate-500">
          For every call, the configuration is chosen by the first rule that applies:
        </p>
        <ol className="mt-2 list-decimal space-y-1 pl-5 text-sm text-slate-700">
          {view.resolution_order.map((tier) => (
            <li key={tier}>{tier}</li>
          ))}
        </ol>
      </div>

      {view.directions.map((state) => (
        <DirectionCard
          key={state.direction}
          state={state}
          eligible={eligible}
          canEdit={isAdmin}
          pending={setDefault.isPending}
          onSelect={change}
        />
      ))}

      <BuiltinFallbackPanel config={view.builtin_fallback} />
    </div>
  );
}
