import { useState } from "react";
import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";
import { Spinner } from "../../components/ui/spinner";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import type { ScheduleResponse, ScheduleSlot } from "../../types/api";
import { ScheduleFormDialog } from "./ScheduleFormDialog";
import { useContactSchedules, useDeleteSchedule, useUpdateSchedule } from "./hooks";

function fmtWindow(s: ScheduleResponse): string {
  return `${s.window_start_local.slice(0, 5)}–${s.window_end_local.slice(0, 5)}`;
}

export function ScheduleSection({ contactId }: { contactId: string }) {
  const list = useContactSchedules(contactId);
  const update = useUpdateSchedule();
  const del = useDeleteSchedule();
  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<ScheduleResponse | null>(null);
  const [toDelete, setToDelete] = useState<ScheduleResponse | null>(null);

  const schedules = list.data ?? [];
  const takenSlots = schedules.map((s) => s.slot) as ScheduleSlot[];
  const hasFreeSlot = takenSlots.length < 2;

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="font-display text-lg text-ink-strong">Schedules</h2>
        {hasFreeSlot ? (
          <Button variant="secondary" onClick={() => setCreateOpen(true)}>
            + Add schedule
          </Button>
        ) : null}
      </div>

      {list.isLoading ? (
        <div className="flex items-center gap-2 text-muted">
          <Spinner /> Loading…
        </div>
      ) : schedules.length === 0 ? (
        <p className="text-sm text-faint">No schedules yet.</p>
      ) : (
        <ul className="space-y-2">
          {schedules.map((s) => (
            <li
              key={s.id}
              className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line px-3 py-2"
            >
              <div className="text-sm">
                <span className="font-medium capitalize">{s.slot}</span>{" "}
                <span className="text-muted">
                  {fmtWindow(s)} · {s.days_of_week.length} days
                </span>{" "}
                {s.enabled ? <Badge tone="green">on</Badge> : <Badge>off</Badge>}
              </div>
              <div className="flex gap-2">
                <Button
                  variant="ghost"
                  disabled={update.isPending}
                  onClick={() => update.mutate({ id: s.id, body: { enabled: !s.enabled } })}
                >
                  {s.enabled ? "Disable" : "Enable"}
                </Button>
                <Button variant="secondary" onClick={() => setEditing(s)}>
                  Edit
                </Button>
                <Button variant="danger" onClick={() => setToDelete(s)}>
                  Delete
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {createOpen ? (
        <ScheduleFormDialog
          mode="create"
          contactId={contactId}
          existingSlots={takenSlots}
          onClose={() => setCreateOpen(false)}
        />
      ) : null}
      {editing ? (
        <ScheduleFormDialog
          mode="edit"
          contactId={contactId}
          schedule={editing}
          existingSlots={takenSlots}
          onClose={() => setEditing(null)}
        />
      ) : null}
      <ConfirmDialog
        open={toDelete !== null}
        title="Delete schedule?"
        body={
          <>
            Delete the <strong>{toDelete?.slot}</strong> schedule? Future automatic calls for this
            slot stop. This cannot be undone.
          </>
        }
        confirmLabel="Delete"
        busy={del.isPending}
        onCancel={() => setToDelete(null)}
        onConfirm={() => {
          if (!toDelete) return;
          del.mutate(toDelete.id, { onSuccess: () => setToDelete(null) });
        }}
      />
    </section>
  );
}
