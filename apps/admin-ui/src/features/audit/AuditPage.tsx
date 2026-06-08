import { useMemo, useState } from "react";
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
  const [actorFilter, setActorFilter] = useState("");
  const [actionFilter, setActionFilter] = useState("");
  const { data, isLoading, isError, error } = useAudit(limit);

  const entries = useMemo(() => data ?? [], [data]);
  const actions = useMemo(
    () => [...new Set(entries.map((e) => e.action))].sort(),
    [entries],
  );

  const filtered = useMemo(() => {
    const actor = actorFilter.trim().toLowerCase();
    return entries.filter((e) => {
      if (actionFilter && e.action !== actionFilter) return false;
      if (actor && !e.actor_email.toLowerCase().includes(actor)) return false;
      return true;
    });
  }, [entries, actorFilter, actionFilter]);

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Audit log</h1>

      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-600" htmlFor="audit-actor">
            Actor
          </label>
          <Input
            id="audit-actor"
            className="w-56"
            placeholder="filter by email"
            value={actorFilter}
            onChange={(e) => setActorFilter(e.target.value)}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-600" htmlFor="audit-action">
            Action
          </label>
          <Select
            id="audit-action"
            className="w-56"
            value={actionFilter}
            onChange={(e) => setActionFilter(e.target.value)}
          >
            <option value="">All actions</option>
            {actions.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-600" htmlFor="audit-limit">
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
        <div className="flex items-center gap-2 text-gray-600">
          <Spinner /> Loading audit log…
        </div>
      ) : isError ? (
        <p className="text-sm text-red-700">Failed to load audit log: {(error as Error)?.message}</p>
      ) : (
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
            {filtered.length === 0 ? (
              <Tr>
                <Td className="text-gray-500" colSpan={5}>
                  No matching entries.
                </Td>
              </Tr>
            ) : null}
            {filtered.map((e) => (
              <Tr key={e.id}>
                <Td className="whitespace-nowrap text-xs">{fmtDate(e.created_at)}</Td>
                <Td className="text-xs">{e.actor_email}</Td>
                <Td className="font-mono text-xs">{e.action}</Td>
                <Td className="text-xs">
                  {e.entity_type ? (
                    <>
                      <span className="text-gray-500">{e.entity_type}</span>
                      {e.entity_id ? (
                        <span className="ml-1 font-mono text-gray-700">{e.entity_id}</span>
                      ) : null}
                    </>
                  ) : (
                    <span className="text-gray-400">—</span>
                  )}
                </Td>
                <Td className="max-w-md truncate font-mono text-xs" title={fmtDetail(e.detail)}>
                  {fmtDetail(e.detail)}
                </Td>
              </Tr>
            ))}
          </Tbody>
        </Table>
      )}
    </div>
  );
}
