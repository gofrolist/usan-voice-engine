import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useIsAdmin } from "../../auth/useSession";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Button } from "../../components/ui/button";
import { Select } from "../../components/ui/select";
import { Spinner } from "../../components/ui/spinner";
import { Tabs } from "../../components/ui/tabs";
import type { QueuesSummary, QueueStatus } from "../../types/api";
import {
  PAGE_SIZE,
  useCallbackRequests,
  useFollowUpFlags,
  useQueuesSummary,
  useUpdateCallbackStatus,
  useUpdateFlagStatus,
} from "./hooks";
import { QueueTable } from "./QueueTable";

const STATUS_VALUES = new Set(["open", "acknowledged", "resolved", "all"]);
const SEVERITY_VALUES = new Set(["urgent", "routine"]);

function flagsTabLabel(s: QueuesSummary | undefined): string {
  if (!s) return "Follow-up flags";
  const urgent = s.flags_open_urgent > 0 ? `, ${s.flags_open_urgent} urgent` : "";
  return `Follow-up flags (${s.flags_open} open${urgent})`;
}

function callbacksTabLabel(s: QueuesSummary | undefined): string {
  return s ? `Callbacks (${s.callbacks_open} open)` : "Callbacks";
}

function Pagination({
  count,
  offset,
  onOffsetChange,
}: {
  count: number;
  offset: number;
  onOffsetChange: (offset: number) => void;
}) {
  // A full page means there may be more rows; a short page is the last one.
  const hasNext = count === PAGE_SIZE;
  const hasPrev = offset > 0;
  const rangeStart = count === 0 ? 0 : offset + 1;
  const rangeEnd = offset + count;
  return (
    <div className="flex items-center gap-3 text-sm text-muted">
      <Button
        variant="secondary"
        disabled={!hasPrev}
        onClick={() => onOffsetChange(Math.max(0, offset - PAGE_SIZE))}
      >
        Previous
      </Button>
      <span>
        {rangeStart}–{rangeEnd}
      </span>
      <Button
        variant="secondary"
        disabled={!hasNext}
        onClick={() => onOffsetChange(offset + PAGE_SIZE)}
      >
        Next
      </Button>
    </div>
  );
}

interface TabCommonProps {
  status: QueueStatus | undefined;
  offset: number;
  onOffsetChange: (offset: number) => void;
  // The manual Refresh affordance re-runs the summary query alongside the list.
  refreshSummary: () => void;
}

// Each tab mounts only while active, so the inactive queue's list GET (and its
// server-side audit row) never fires for a tab nobody is looking at.
function FlagsTab({
  status,
  severity,
  offset,
  onOffsetChange,
  refreshSummary,
  defaultView,
}: TabCommonProps & { severity: string | undefined; defaultView: boolean }) {
  const isAdmin = useIsAdmin();
  const flags = useFollowUpFlags(status, severity, PAGE_SIZE, offset);
  const transition = useUpdateFlagStatus();
  const [resolveId, setResolveId] = useState<number | null>(null);

  if (flags.isLoading) {
    return (
      <div className="flex items-center gap-2 text-slate-600">
        <Spinner /> Loading flags…
      </div>
    );
  }
  if (flags.isError) {
    return (
      <p className="text-sm text-red-700">
        Failed to load flags: {(flags.error as Error)?.message}
      </p>
    );
  }

  const list = flags.data ?? [];
  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <Button
          variant="secondary"
          onClick={() => {
            void flags.refetch();
            refreshSummary();
          }}
        >
          Refresh
        </Button>
      </div>
      <QueueTable
        kind="flags"
        rows={list}
        isAdmin={isAdmin}
        pending={transition.isPending}
        emptyMessage={
          defaultView ? "No open follow-up flags — all clear." : "No flags match these filters"
        }
        onAcknowledge={(id) => transition.mutate({ id, status: "acknowledged" })}
        onResolve={(id) => setResolveId(id)}
      />
      <Pagination count={list.length} offset={offset} onOffsetChange={onOffsetChange} />
      <ConfirmDialog
        open={resolveId !== null}
        title="Resolve flag?"
        body="Resolution is one-way — the flag leaves the triage queue."
        confirmLabel="Resolve"
        busy={transition.isPending}
        onConfirm={() => {
          if (resolveId !== null) {
            transition.mutate(
              { id: resolveId, status: "resolved" },
              { onSettled: () => setResolveId(null) },
            );
          }
        }}
        onCancel={() => setResolveId(null)}
      />
    </div>
  );
}

function CallbacksTab({ status, offset, onOffsetChange, refreshSummary }: TabCommonProps) {
  const isAdmin = useIsAdmin();
  const callbacks = useCallbackRequests(status, PAGE_SIZE, offset);
  const transition = useUpdateCallbackStatus();
  const [resolveId, setResolveId] = useState<number | null>(null);

  if (callbacks.isLoading) {
    return (
      <div className="flex items-center gap-2 text-slate-600">
        <Spinner /> Loading callbacks…
      </div>
    );
  }
  if (callbacks.isError) {
    return (
      <p className="text-sm text-red-700">
        Failed to load callbacks: {(callbacks.error as Error)?.message}
      </p>
    );
  }

  const list = callbacks.data ?? [];
  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <Button
          variant="secondary"
          onClick={() => {
            void callbacks.refetch();
            refreshSummary();
          }}
        >
          Refresh
        </Button>
      </div>
      <QueueTable
        kind="callbacks"
        rows={list}
        isAdmin={isAdmin}
        pending={transition.isPending}
        emptyMessage="No callback requests match."
        onAcknowledge={(id) => transition.mutate({ id, status: "acknowledged" })}
        onResolve={(id) => setResolveId(id)}
      />
      <Pagination count={list.length} offset={offset} onOffsetChange={onOffsetChange} />
      <ConfirmDialog
        open={resolveId !== null}
        title="Resolve callback?"
        body="Resolving records that the callback was handled — the nurse dials out-of-band."
        confirmLabel="Resolve"
        busy={transition.isPending}
        onConfirm={() => {
          if (resolveId !== null) {
            transition.mutate(
              { id: resolveId, status: "resolved" },
              { onSettled: () => setResolveId(null) },
            );
          }
        }}
        onCancel={() => setResolveId(null)}
      />
    </div>
  );
}

// Tab, status, severity and offset all live in the URL search params so
// back-navigation from a call detail restores the nurse's exact position
// (spec §5.4). Junk param values fall back to the defaults at this boundary.
export function QueuesPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = searchParams.get("tab") === "callbacks" ? "callbacks" : "flags";
  const rawStatus = searchParams.get("status") ?? "open"; // Open is the triage default
  const statusParam = STATUS_VALUES.has(rawStatus) ? rawStatus : "open";
  const rawSeverity = searchParams.get("severity") ?? "";
  const severityParam = SEVERITY_VALUES.has(rawSeverity) ? rawSeverity : "";
  // Junk offsets (?offset=abc, ?offset=1.5) clamp to 0 here instead of being
  // forwarded to the server as a raw 422 — same boundary rule as tab/status.
  const rawOffset = Number(searchParams.get("offset"));
  const offset = Number.isInteger(rawOffset) && rawOffset > 0 ? rawOffset : 0;

  const status = statusParam === "all" ? undefined : (statusParam as QueueStatus);
  const severity = severityParam || undefined;

  const summary = useQueuesSummary();

  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(searchParams);
    for (const [key, value] of Object.entries(changes)) {
      if (value === null) next.delete(key);
      else next.set(key, value);
    }
    setSearchParams(next);
  };

  return (
    <div className="space-y-4">
      <h1 className="font-display text-2xl text-ink-strong">Queues</h1>

      <Tabs
        className="flex-row"
        items={[
          { key: "flags", label: flagsTabLabel(summary.data) },
          { key: "callbacks", label: callbacksTabLabel(summary.data) },
        ]}
        active={tab}
        onSelect={(key) => update({ tab: key, offset: null })}
      />

      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-muted" htmlFor="queues-status">
            Status
          </label>
          <Select
            id="queues-status"
            className="w-40"
            value={statusParam}
            onChange={(e) => update({ status: e.target.value, offset: null })}
          >
            <option value="open">Open</option>
            <option value="acknowledged">Acknowledged</option>
            <option value="resolved">Resolved</option>
            <option value="all">All</option>
          </Select>
        </div>
        {tab === "flags" ? (
          <div>
            <label
              className="mb-1 block text-xs font-medium text-muted"
              htmlFor="queues-severity"
            >
              Severity
            </label>
            <Select
              id="queues-severity"
              className="w-36"
              value={severityParam}
              onChange={(e) => update({ severity: e.target.value || null, offset: null })}
            >
              <option value="">All</option>
              <option value="urgent">Urgent</option>
              <option value="routine">Routine</option>
            </Select>
          </div>
        ) : null}
      </div>

      {tab === "flags" ? (
        <FlagsTab
          status={status}
          severity={severity}
          offset={offset}
          onOffsetChange={(o) => update({ offset: String(o) })}
          refreshSummary={() => void summary.refetch()}
          defaultView={statusParam === "open" && !severity}
        />
      ) : (
        <CallbacksTab
          status={status}
          offset={offset}
          onOffsetChange={(o) => update({ offset: String(o) })}
          refreshSummary={() => void summary.refetch()}
        />
      )}
    </div>
  );
}
