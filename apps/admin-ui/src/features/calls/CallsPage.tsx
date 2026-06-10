import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Select } from "../../components/ui/select";
import { Input } from "../../components/ui/input";
import { Badge } from "../../components/ui/badge";
import { Spinner } from "../../components/ui/spinner";
import { Button } from "../../components/ui/button";
import { fmtDate, fmtDuration } from "../../lib/format";
import type { AdminCallSummary } from "../../types/api";
import { PAGE_SIZE, useCalls, type CallsFilters } from "./hooks";

// The 11 CallStatus values, mirrored from apps/api db/base.py CallStatus.
const CALL_STATUSES = [
  "queued",
  "dialing",
  "ringing",
  "in_progress",
  "completed",
  "voicemail_left",
  "no_answer",
  "busy",
  "failed",
  "dnc_blocked",
  "cancelled",
] as const;

type BadgeTone = "gray" | "green" | "blue" | "red" | "amber";

function statusTone(status: string): BadgeTone {
  if (status === "completed") return "green";
  if (status === "in_progress") return "blue";
  if (status === "failed" || status === "dnc_blocked") return "red";
  if (
    status === "no_answer" ||
    status === "busy" ||
    status === "voicemail_left" ||
    status === "cancelled"
  ) {
    return "amber";
  }
  return "gray"; // queued, dialing, ringing
}

// Origin badge: explicit sched/batch keys; a NULL origin is an inbound call when
// direction says so, otherwise an ad-hoc (operator-enqueued) outbound call.
function originBadge(c: AdminCallSummary) {
  if (c.origin?.source === "schedule") return <Badge tone="blue">Schedule</Badge>;
  if (c.origin?.source === "batch") return <Badge tone="amber">Batch</Badge>;
  if (c.direction === "inbound") return <Badge tone="green">Inbound</Badge>;
  return <Badge>Ad hoc</Badge>;
}

// `created_to` is exclusive server-side; the field is labeled "To (inclusive)", so
// the selected date is bumped by one day before being sent (spec §5.2) — otherwise
// To=2026-06-10 would silently drop June 10's calls.
function nextDay(date: string): string {
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
}

export function CallsPage() {
  const navigate = useNavigate();
  // elder_id is deep-linked from queue rows and call detail; no picker this phase.
  const [searchParams] = useSearchParams();
  const elderId = searchParams.get("elder_id") ?? undefined;

  const [status, setStatus] = useState("");
  const [direction, setDirection] = useState("");
  const [origin, setOrigin] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [offset, setOffset] = useState(0);

  // Every filter change resets paging to the first page.
  const setFilter = (set: (v: string) => void) => (value: string) => {
    set(value);
    setOffset(0);
  };

  const filters: CallsFilters = {
    elderId,
    status: status || undefined,
    direction: direction || undefined,
    origin: origin || undefined,
    createdFrom: from || undefined,
    createdTo: to ? nextDay(to) : undefined,
  };
  const calls = useCalls(filters, PAGE_SIZE, offset);

  const list = calls.data ?? [];
  // A full page means there may be more rows; a short page is the last one.
  const hasNext = list.length === PAGE_SIZE;
  const hasPrev = offset > 0;
  const rangeStart = list.length === 0 ? 0 : offset + 1;
  const rangeEnd = offset + list.length;

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Calls</h1>

      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="calls-status">
            Status
          </label>
          <Select
            id="calls-status"
            className="w-40"
            value={status}
            onChange={(e) => setFilter(setStatus)(e.target.value)}
          >
            <option value="">All</option>
            {CALL_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <label
            className="mb-1 block text-xs font-medium text-slate-600"
            htmlFor="calls-direction"
          >
            Direction
          </label>
          <Select
            id="calls-direction"
            className="w-36"
            value={direction}
            onChange={(e) => setFilter(setDirection)(e.target.value)}
          >
            <option value="">All</option>
            <option value="outbound">Outbound</option>
            <option value="inbound">Inbound</option>
          </Select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="calls-origin">
            Origin
          </label>
          <Select
            id="calls-origin"
            className="w-36"
            value={origin}
            onChange={(e) => setFilter(setOrigin)(e.target.value)}
          >
            <option value="">All</option>
            <option value="schedule">Schedule</option>
            <option value="batch">Batch</option>
            <option value="adhoc">Ad hoc</option>
          </Select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="calls-from">
            From
          </label>
          <Input
            id="calls-from"
            type="date"
            className="w-40"
            value={from}
            onChange={(e) => setFilter(setFrom)(e.target.value)}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="calls-to">
            To (inclusive)
          </label>
          <Input
            id="calls-to"
            type="date"
            className="w-40"
            value={to}
            onChange={(e) => setFilter(setTo)(e.target.value)}
          />
        </div>
      </div>

      {calls.isLoading ? (
        <div className="flex items-center gap-2 text-slate-600">
          <Spinner /> Loading calls…
        </div>
      ) : calls.isError ? (
        <p className="text-sm text-red-700">
          Failed to load calls: {(calls.error as Error)?.message}
        </p>
      ) : (
        <>
          <Table>
            <Thead>
              <Tr>
                <Th>Created</Th>
                <Th>Elder</Th>
                <Th>Direction</Th>
                <Th>Origin</Th>
                <Th>Status</Th>
                <Th>Attempt</Th>
                <Th>Duration</Th>
                <Th>Recording</Th>
              </Tr>
            </Thead>
            <Tbody>
              {list.length === 0 ? (
                <Tr>
                  <Td className="text-slate-500" colSpan={8}>
                    No calls match these filters
                  </Td>
                </Tr>
              ) : null}
              {list.map((c) => (
                <Tr
                  key={c.id}
                  className="cursor-pointer"
                  onClick={() => navigate(`/calls/${c.id}`)}
                >
                  <Td className="whitespace-nowrap text-xs">{fmtDate(c.created_at)}</Td>
                  <Td>
                    <div className="font-medium text-slate-900">{c.elder_name ?? "—"}</div>
                    <div className="font-mono text-xs text-slate-500">{c.masked_phone}</div>
                  </Td>
                  <Td className="text-xs">{c.direction}</Td>
                  <Td>{originBadge(c)}</Td>
                  <Td>
                    <Badge tone={statusTone(c.status)}>{c.status}</Badge>
                  </Td>
                  <Td className="text-xs">{c.attempt}</Td>
                  <Td className="font-mono text-xs">{fmtDuration(c.duration_seconds)}</Td>
                  <Td className="text-xs">
                    {c.has_recording ? <span aria-label="has recording">●</span> : null}
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </Table>

          <div className="flex items-center gap-3 text-sm text-slate-600">
            <Button
              variant="secondary"
              disabled={!hasPrev}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              Previous
            </Button>
            <span>
              {rangeStart}–{rangeEnd}
            </span>
            <Button
              variant="secondary"
              disabled={!hasNext}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
