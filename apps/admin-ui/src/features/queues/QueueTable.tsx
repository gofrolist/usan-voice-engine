import { Link } from "react-router-dom";
import { Button } from "../../components/ui/button";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { cn } from "../../lib/cn";
import { fmtDate } from "../../lib/format";
import type { CallbackRequestSummary, FollowupFlagSummary, QueueStatus } from "../../types/api";

interface QueueActionProps {
  // Viewers get NO mutation affordances at all — hidden, not disabled (spec §5.4).
  isAdmin: boolean;
  // Double-click guard while a transition is in flight (the server is also idempotent).
  pending: boolean;
  emptyMessage: string;
  onAcknowledge: (id: number) => void;
  // Opens the caller's ConfirmDialog — resolution is one-way.
  onResolve: (id: number) => void;
}

type QueueTableProps =
  | ({ kind: "flags"; rows: FollowupFlagSummary[] } & QueueActionProps)
  | ({ kind: "callbacks"; rows: CallbackRequestSummary[] } & QueueActionProps);

// Filled red for urgent, outline for routine — the visual weight keys off the
// same severity value the row exposes as data-severity (tests assert the
// attribute + badge text, never these classes).
function SeverityBadge({ severity }: { severity: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium",
        severity === "urgent"
          ? "bg-red-600 text-white"
          : "border border-slate-300 bg-white text-slate-600",
      )}
    >
      {severity}
    </span>
  );
}

// The console never shows a dialable number: elder identity is name + masked
// phone only, with the name deep-linking to that elder's call history.
function ElderCell({
  elderId,
  name,
  maskedPhone,
}: {
  elderId: string;
  name: string | null;
  maskedPhone: string;
}) {
  return (
    <Td>
      <Link
        className="font-medium text-slate-900 hover:underline"
        to={`/calls?elder_id=${elderId}`}
      >
        {name ?? "—"}
      </Link>
      <div className="font-mono text-xs text-slate-500">{maskedPhone}</div>
    </Td>
  );
}

function StatusCell({
  status,
  updatedBy,
  updatedAt,
}: {
  status: QueueStatus;
  updatedBy: string | null;
  updatedAt: string | null;
}) {
  return (
    <Td>
      <div className="text-xs">{status}</div>
      {updatedBy ? (
        <div className="text-xs text-slate-500">
          {updatedBy}
          {updatedAt ? ` · ${fmtDate(updatedAt)}` : ""}
        </div>
      ) : null}
    </Td>
  );
}

function ViewCallCell({ callId }: { callId: string }) {
  return (
    <Td className="whitespace-nowrap">
      <Link className="text-xs text-indigo-600 hover:underline" to={`/calls/${callId}`}>
        View call
      </Link>
    </Td>
  );
}

// Acknowledge only from open; Resolve from open/acknowledged (one-way, confirmed
// upstream). Resolved rows get no actions.
function ActionsCell({
  id,
  status,
  pending,
  onAcknowledge,
  onResolve,
}: {
  id: number;
  status: QueueStatus;
  pending: boolean;
  onAcknowledge: (id: number) => void;
  onResolve: (id: number) => void;
}) {
  return (
    <Td className="whitespace-nowrap">
      <div className="flex gap-2">
        {status === "open" ? (
          <Button variant="secondary" disabled={pending} onClick={() => onAcknowledge(id)}>
            Acknowledge
          </Button>
        ) : null}
        {status === "open" || status === "acknowledged" ? (
          <Button variant="danger" disabled={pending} onClick={() => onResolve(id)}>
            Resolve
          </Button>
        ) : null}
      </div>
    </Td>
  );
}

// Generic queue table shared by the flags and callbacks tabs (spec §5.4).
export function QueueTable(props: QueueTableProps) {
  const { isAdmin, pending, emptyMessage, onAcknowledge, onResolve } = props;
  const cols = (props.kind === "flags" ? 7 : 6) + (isAdmin ? 1 : 0);

  return (
    <Table>
      <Thead>
        <Tr>
          <Th>Created</Th>
          <Th>Elder</Th>
          {props.kind === "flags" ? (
            <>
              <Th>Severity</Th>
              <Th>Category</Th>
              <Th>Reason</Th>
            </>
          ) : (
            <>
              <Th>Requested time</Th>
              <Th>Notes</Th>
            </>
          )}
          <Th>Status</Th>
          <Th>Call</Th>
          {isAdmin ? <Th>Actions</Th> : null}
        </Tr>
      </Thead>
      <Tbody>
        {props.rows.length === 0 ? (
          <Tr>
            <Td className="text-slate-500" colSpan={cols}>
              {emptyMessage}
            </Td>
          </Tr>
        ) : null}
        {props.kind === "flags"
          ? props.rows.map((f) => (
              <Tr
                key={f.id}
                data-severity={f.severity}
                // Urgent styling keys off the same severity value as data-severity.
                className={cn(f.severity === "urgent" && "border-l-4 border-l-red-500")}
              >
                <Td className="whitespace-nowrap text-xs">{fmtDate(f.created_at)}</Td>
                <ElderCell elderId={f.elder_id} name={f.elder_name} maskedPhone={f.masked_phone} />
                <Td>
                  <SeverityBadge severity={f.severity} />
                </Td>
                <Td className="text-xs">{f.category}</Td>
                <Td className="max-w-md">{f.reason ?? "—"}</Td>
                <StatusCell
                  status={f.status}
                  updatedBy={f.status_updated_by}
                  updatedAt={f.status_updated_at}
                />
                <ViewCallCell callId={f.call_id} />
                {isAdmin ? (
                  <ActionsCell
                    id={f.id}
                    status={f.status}
                    pending={pending}
                    onAcknowledge={onAcknowledge}
                    onResolve={onResolve}
                  />
                ) : null}
              </Tr>
            ))
          : props.rows.map((c) => (
              <Tr key={c.id}>
                <Td className="whitespace-nowrap text-xs">{fmtDate(c.created_at)}</Td>
                <ElderCell elderId={c.elder_id} name={c.elder_name} maskedPhone={c.masked_phone} />
                <Td>
                  <div>{c.requested_time_text}</div>
                  {c.requested_at ? (
                    <div className="text-xs text-slate-500">{fmtDate(c.requested_at)}</div>
                  ) : null}
                </Td>
                <Td className="max-w-md">{c.notes ?? "—"}</Td>
                <StatusCell
                  status={c.status}
                  updatedBy={c.status_updated_by}
                  updatedAt={c.status_updated_at}
                />
                <ViewCallCell callId={c.call_id} />
                {isAdmin ? (
                  <ActionsCell
                    id={c.id}
                    status={c.status}
                    pending={pending}
                    onAcknowledge={onAcknowledge}
                    onResolve={onResolve}
                  />
                ) : null}
              </Tr>
            ))}
      </Tbody>
    </Table>
  );
}
