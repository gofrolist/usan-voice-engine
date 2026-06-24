import { useState } from "react";
import { Link } from "react-router-dom";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Select } from "../../components/ui/select";
import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";
import { Spinner } from "../../components/ui/spinner";
import { fmtDate } from "../../lib/format";
import { useIsAdmin } from "../../auth/useSession";
import { useSchedules, type ScheduleFilters } from "./hooks";

const PAGE_SIZE = 100;

export function SchedulesPage() {
  const isAdmin = useIsAdmin();
  const [slot, setSlot] = useState("");
  const [lastResult, setLastResult] = useState("");
  const [offset, setOffset] = useState(0);

  const setFilter = (set: (v: string) => void) => (value: string) => {
    set(value);
    setOffset(0);
  };

  const filters: ScheduleFilters = {
    slot: slot || undefined,
    lastResult: lastResult || undefined,
  };
  const schedules = useSchedules(filters, PAGE_SIZE, offset);

  if (!isAdmin) return <p className="text-sm text-muted">Admins only.</p>;

  const list = schedules.data ?? [];
  const hasNext = list.length === PAGE_SIZE;
  const hasPrev = offset > 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl text-ink-strong">Schedules</h1>
        <Button
          variant={lastResult === "skipped_window" ? "primary" : "secondary"}
          onClick={() =>
            setFilter(setLastResult)(lastResult === "skipped_window" ? "" : "skipped_window")
          }
        >
          Who missed (skipped window)
        </Button>
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="sch-slot">
            Slot
          </label>
          <Select
            id="sch-slot"
            className="w-36"
            value={slot}
            onChange={(e) => setFilter(setSlot)(e.target.value)}
          >
            <option value="">All</option>
            <option value="morning">morning</option>
            <option value="evening">evening</option>
          </Select>
        </div>
      </div>

      {schedules.isLoading ? (
        <div className="flex items-center gap-2 text-muted">
          <Spinner /> Loading schedules…
        </div>
      ) : schedules.isError ? (
        <p className="text-sm text-red-700">
          Failed to load schedules: {(schedules.error as Error)?.message}
        </p>
      ) : (
        <>
          <Table>
            <Thead>
              <Tr>
                <Th>Contact</Th>
                <Th>Slot</Th>
                <Th>Window</Th>
                <Th>Days</Th>
                <Th>Enabled</Th>
                <Th>Next run</Th>
                <Th>Last result</Th>
              </Tr>
            </Thead>
            <Tbody>
              {list.length === 0 ? (
                <Tr>
                  <Td className="text-faint" colSpan={7}>
                    No schedules match these filters.
                  </Td>
                </Tr>
              ) : null}
              {list.map((s) => (
                <Tr key={s.id}>
                  <Td className="font-medium">
                    <Link className="text-accent hover:underline" to={`/contacts/${s.contact_id}`}>
                      {s.contact_name || s.contact_id}
                    </Link>
                  </Td>
                  <Td className="capitalize">{s.slot}</Td>
                  <Td className="font-mono text-xs">
                    {s.window_start_local.slice(0, 5)}–{s.window_end_local.slice(0, 5)}
                  </Td>
                  <Td className="text-xs">{s.days_of_week.map((d) => d.slice(0, 3)).join(", ")}</Td>
                  <Td>{s.enabled ? <Badge tone="green">on</Badge> : <Badge>off</Badge>}</Td>
                  <Td className="whitespace-nowrap text-xs">{fmtDate(s.next_run_at)}</Td>
                  <Td className="text-xs">{s.last_result ?? "—"}</Td>
                </Tr>
              ))}
            </Tbody>
          </Table>

          <div className="flex items-center gap-3 text-sm text-muted">
            <Button
              variant="secondary"
              disabled={!hasPrev}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              Previous
            </Button>
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
