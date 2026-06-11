import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { Badge } from "../../components/ui/badge";
import { Spinner } from "../../components/ui/spinner";
import type { ApiError } from "../../lib/api";
import { fmtDate, fmtDuration } from "../../lib/format";
import type { AdminCallDetail } from "../../types/api";
import { RecordingPlayer } from "./RecordingPlayer";
import { TranscriptViewer } from "./TranscriptViewer";
import { useCall } from "./hooks";

// Same null-origin semantics as the CallsPage origin badge: an explicit sched/batch
// key wins; otherwise inbound calls are "Inbound" and outbound ones "Ad hoc".
function originLabel(c: AdminCallDetail): string {
  if (c.origin?.source === "schedule") return "Schedule";
  if (c.origin?.source === "batch") return "Batch";
  return c.direction === "inbound" ? "Inbound" : "Ad hoc";
}

function fmtMaybeDate(iso: string | null): string {
  return iso === null ? "—" : fmtDate(iso);
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium text-slate-500">{label}</dt>
      <dd className="text-sm text-slate-900">{children}</dd>
    </div>
  );
}

export function CallDetailPage() {
  const { id = "" } = useParams();
  const call = useCall(id);

  if (call.isLoading) {
    return (
      <div className="flex items-center gap-2 text-slate-600">
        <Spinner /> Loading call…
      </div>
    );
  }
  if (call.isError) {
    // Stale queue links happen — a 404 gets its own copy instead of a generic error.
    if ((call.error as ApiError).status === 404) {
      return (
        <div className="space-y-2">
          <h1 className="text-xl font-semibold">Call not found</h1>
          <p className="text-sm text-slate-500">
            This call does not exist — the link may be stale.{" "}
            <Link to="/calls" className="text-indigo-600 hover:underline">
              Back to calls
            </Link>
          </p>
        </div>
      );
    }
    return (
      <p className="text-sm text-red-700">Failed to load call: {(call.error as Error)?.message}</p>
    );
  }

  const c = call.data;
  if (!c) return null;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Call detail</h1>
        <Link to="/calls" className="text-sm text-indigo-600 hover:underline">
          ← Back to calls
        </Link>
      </div>

      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <div className="flex items-baseline gap-3">
          {c.elder_id !== null ? (
            <Link
              to={`/calls?elder_id=${c.elder_id}`}
              className="text-lg font-medium text-indigo-600 hover:underline"
              title="View this elder's calls"
            >
              {c.elder_name ?? "Unknown elder"}
            </Link>
          ) : (
            <span className="text-lg font-medium text-slate-900">{c.elder_name ?? "—"}</span>
          )}
          <span className="font-mono text-sm text-slate-500">{c.masked_phone}</span>
        </div>

        <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
          <Fact label="Direction">{c.direction}</Fact>
          <Fact label="Status">
            <Badge>{c.status}</Badge>
          </Fact>
          <Fact label="Origin">
            <Badge>{originLabel(c)}</Badge>
          </Fact>
          <Fact label="Attempt">
            {c.parent_call_id !== null ? (
              <Link to={`/calls/${c.parent_call_id}`} className="text-indigo-600 hover:underline">
                attempt {c.attempt} — view parent
              </Link>
            ) : (
              <>attempt {c.attempt}</>
            )}
          </Fact>
          <Fact label="Created">{fmtDate(c.created_at)}</Fact>
          <Fact label="Started">{fmtMaybeDate(c.started_at)}</Fact>
          <Fact label="Answered">{fmtMaybeDate(c.answered_at)}</Fact>
          <Fact label="Ended">{fmtMaybeDate(c.ended_at)}</Fact>
          <Fact label="Duration">{fmtDuration(c.duration_seconds)}</Fact>
          <Fact label="End reason">{c.end_reason ?? "—"}</Fact>
        </dl>
      </div>

      <section className="space-y-2">
        <h2 className="text-sm font-semibold text-slate-700">Recording</h2>
        <RecordingPlayer
          url={c.presigned_recording_url}
          ttlS={c.recording_url_ttl_s}
          hasRecording={c.has_recording}
          callStatus={c.status}
        />
      </section>

      <section className="space-y-2">
        <h2 className="text-sm font-semibold text-slate-700">Transcript</h2>
        <TranscriptViewer segments={c.transcript} callStatus={c.status} />
      </section>
    </div>
  );
}
