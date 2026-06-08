import { useEffect, useMemo, useState } from "react";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Input } from "../../components/ui/input";
import { Select } from "../../components/ui/select";
import { Spinner } from "../../components/ui/spinner";
import { useAudit } from "./hooks";

const LIMITS = [50, 100, 200, 500] as const;

function fmtDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function fmtDetail(detail: Record<string, unknown>): string {
  const keys = Object.keys(detail);
  if (keys.length === 0) return "";
  return JSON.stringify(detail);
}

export function AuditPage() {
  const [limit, setLimit] = useState<number>(100);
  // actorInput is the live text; `actor` is the debounced value sent to the server so
  // we don't fire a request per keystroke.
  const [actorInput, setActorInput] = useState("");
  const [actor, setActor] = useState("");
  const [action, setAction] = useState("");

  useEffect(() => {
    const t = setTimeout(() => setActor(actorInput.trim()), 300);
    return () => clearTimeout(t);
  }, [actorInput]);

  // Filtering happens SERVER-SIDE (see useAudit) so it spans the whole table, not just
  // the latest `limit` rows.
  const { data, isLoading, isError, error } = useAudit(limit, actor, action);
  const entries = useMemo(() => data ?? [], [data]);

  // Action options come from the current result set; keep the selected action present
  // so it never vanishes from its own filtered view.
  const actionOptions = useMemo(() => {
    const s = new Set(entries.map((e) => e.action));
    if (action) s.add(action);
    return [...s].sort();
  }, [entries, action]);

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Audit log</h1>

      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="audit-actor">
            Actor
          </label>
          <Input
            id="audit-actor"
            className="w-56"
            placeholder="filter by email"
            value={actorInput}
            onChange={(e) => setActorInput(e.target.value)}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="audit-action">
            Action
          </label>
          <Select
            id="audit-action"
            className="w-56"
            value={action}
            onChange={(e) => setAction(e.target.value)}
          >
            <option value="">All actions</option>
            {actionOptions.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="audit-limit">
            Limit
          </label>
          <Select
            id="audit-limit"
            className="w-28"
            value={String(limit)}
            onChange={(e) => setLimit(Number(e.target.value))}
          >
            {LIMITS.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </Select>
        </div>
      </div>

      {isLoading ? (
        <div className="flex items-center gap-2 text-slate-600">
          <Spinner /> Loading audit log…
        </div>
      ) : isError ? (
        <p className="text-sm text-red-700">Failed to load audit log: {(error as Error)?.message}</p>
      ) : (
        <>
          <p className="text-xs text-slate-500">
            Showing the most recent {limit} entries matching the current filters (filters
            are applied across the whole log, not just this page).
          </p>
          <Table>
            <Thead>
              <Tr>
                <Th>When</Th>
                <Th>Actor</Th>
                <Th>Action</Th>
                <Th>Entity</Th>
                <Th>Detail</Th>
              </Tr>
            </Thead>
            <Tbody>
              {entries.length === 0 ? (
                <Tr>
                  <Td className="text-slate-500" colSpan={5}>
                    No matching entries.
                  </Td>
                </Tr>
              ) : null}
              {entries.map((e) => (
                <Tr key={e.id}>
                  <Td className="whitespace-nowrap text-xs">{fmtDate(e.created_at)}</Td>
                  <Td className="text-xs">{e.actor_email}</Td>
                  <Td className="font-mono text-xs">{e.action}</Td>
                  <Td className="text-xs">
                    {e.entity_type ? (
                      <>
                        <span className="text-slate-500">{e.entity_type}</span>
                        {e.entity_id ? (
                          <span className="ml-1 font-mono text-slate-700">{e.entity_id}</span>
                        ) : null}
                      </>
                    ) : (
                      <span className="text-slate-400">—</span>
                    )}
                  </Td>
                  <Td className="max-w-md truncate font-mono text-xs" title={fmtDetail(e.detail)}>
                    {fmtDetail(e.detail)}
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
        </>
      )}
    </div>
  );
}
