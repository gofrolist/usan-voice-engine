import { Select } from "../../components/ui/select";
import { Spinner } from "../../components/ui/spinner";
import { Badge } from "../../components/ui/badge";
import { useIsAdmin } from "../../auth/useSession";
import { useProfiles, useSetDefault } from "../profiles/hooks";
import type { Direction, ProfileSummary } from "../../types/api";

interface DefaultRowProps {
  direction: Direction;
  label: string;
  help: string;
  current: ProfileSummary | undefined;
  // Profiles eligible to become the default (active + published).
  eligible: ProfileSummary[];
  disabled: boolean;
  onChange: (id: string) => void;
}

function DefaultRow({ direction, label, help, current, eligible, disabled, onChange }: DefaultRowProps) {
  return (
    <div className="rounded border border-gray-200 bg-white p-4">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold">{label}</h2>
        {current ? <Badge tone="blue">{current.name}</Badge> : <Badge tone="gray">none</Badge>}
      </div>
      <p className="mt-1 text-xs text-gray-500">{help}</p>
      <div className="mt-3 max-w-sm">
        <Select
          aria-label={`Default ${direction} profile`}
          value={current?.id ?? ""}
          disabled={disabled}
          onChange={(e) => {
            if (e.target.value !== "") onChange(e.target.value);
          }}
        >
          <option value="" disabled>
            {current ? "Change default…" : "Select a profile…"}
          </option>
          {eligible.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </Select>
      </div>
    </div>
  );
}

export function DefaultsPage() {
  const isAdmin = useIsAdmin();
  const { data: profiles, isLoading, isError, error } = useProfiles();
  const setDefault = useSetDefault();

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 text-gray-600">
        <Spinner /> Loading…
      </div>
    );
  }
  if (isError) {
    return <p className="text-sm text-red-700">Failed to load profiles: {(error as Error)?.message}</p>;
  }

  const list = profiles ?? [];
  const inbound = list.find((p) => p.is_default_inbound);
  const outbound = list.find((p) => p.is_default_outbound);
  // Only active, published profiles can be a default.
  const eligible = list.filter((p) => p.status === "active" && p.published_version !== null);

  function change(direction: Direction, id: string): void {
    setDefault.mutate({ id, direction });
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Default profiles</h1>
      {!isAdmin ? (
        <p className="text-sm text-gray-500">Read-only (viewer role). Only admins can change defaults.</p>
      ) : null}
      <DefaultRow
        direction="inbound"
        label="Inbound default"
        help="Used for elder-initiated calls with no per-elder assignment."
        current={inbound}
        eligible={eligible}
        disabled={!isAdmin || setDefault.isPending}
        onChange={(id) => change("inbound", id)}
      />
      <DefaultRow
        direction="outbound"
        label="Outbound default"
        help="Used for scheduled wellness calls with no per-elder assignment."
        current={outbound}
        eligible={eligible}
        disabled={!isAdmin || setDefault.isPending}
        onChange={(id) => change("outbound", id)}
      />
    </div>
  );
}
