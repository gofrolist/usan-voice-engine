import { useState } from "react";
import { Dialog } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Select } from "../../components/ui/select";
import { Button } from "../../components/ui/button";
import { DaysOfWeekPicker } from "../../components/ui/DaysOfWeekPicker";
import {
  KeyValueEditor,
  recordToRows,
  rowsToRecord,
  type KvRow,
} from "../../components/ui/KeyValueEditor";
import { DYNAMIC_VARS_MAX_BYTES, dynamicVarsByteSize } from "../../lib/dynamicVars";
import type { ApiError } from "../../lib/api";
import type {
  CreateScheduleRequest,
  ScheduleResponse,
  ScheduleSlot,
  UpdateScheduleRequest,
  Weekday,
} from "../../types/api";
import { useProfiles } from "../profiles/hooks";
import { useCreateSchedule, useUpdateSchedule } from "./hooks";

const ALL_DAYS: Weekday[] = [
  "monday",
  "tuesday",
  "wednesday",
  "thursday",
  "friday",
  "saturday",
  "sunday",
];
const ALL_SLOTS: ScheduleSlot[] = ["morning", "evening"];

// "HH:MM" or "HH:MM:SS" -> minutes since midnight, for client-side window checks.
function toMinutes(t: string): number | null {
  const m = /^(\d{2}):(\d{2})(?::\d{2})?$/.exec(t.trim());
  if (!m) return null;
  return Number(m[1]) * 60 + Number(m[2]);
}
const QUIET_START = 9 * 60; // 09:00
const QUIET_END = 21 * 60; // 21:00 (exclusive)

interface ScheduleFormDialogProps {
  mode: "create" | "edit";
  contactId: string;
  schedule?: ScheduleResponse; // required for edit
  existingSlots: ScheduleSlot[]; // slots already taken by this contact
  onClose: () => void;
}

export function ScheduleFormDialog({
  mode,
  contactId,
  schedule,
  existingSlots,
  onClose,
}: ScheduleFormDialogProps) {
  const isEdit = mode === "edit";
  const freeSlots = ALL_SLOTS.filter((s) => !existingSlots.includes(s));
  const [slot, setSlot] = useState<ScheduleSlot>(schedule?.slot ?? freeSlots[0] ?? "morning");
  const [start, setStart] = useState((schedule?.window_start_local ?? "").slice(0, 5));
  const [end, setEnd] = useState((schedule?.window_end_local ?? "").slice(0, 5));
  const [days, setDays] = useState<Weekday[]>(schedule?.days_of_week ?? ALL_DAYS);
  const [enabled, setEnabled] = useState(schedule?.enabled ?? true);
  const [override, setOverride] = useState(schedule?.profile_override ?? "");
  const [rows, setRows] = useState<KvRow[]>(recordToRows(schedule?.dynamic_vars ?? {}));
  const [localError, setLocalError] = useState<string | null>(null);

  const profiles = useProfiles();
  const create = useCreateSchedule();
  const update = useUpdateSchedule();
  const busy = create.isPending || update.isPending;
  const serverError =
    (create.error as ApiError | null)?.detail ?? (update.error as ApiError | null)?.detail;
  const live = (profiles.data ?? []).filter((p) => p.status === "active");

  function validate(): string | null {
    const s = toMinutes(start);
    const e = toMinutes(end);
    if (s === null || e === null) return "Enter a window start and end (HH:MM).";
    if (s >= e) return "Window start must be before the end.";
    if (e <= QUIET_START || s >= QUIET_END) return "Window must fall within 09:00–21:00.";
    if (days.length === 0) return "Pick at least one day.";
    if (dynamicVarsByteSize(rowsToRecord(rows)) > DYNAMIC_VARS_MAX_BYTES) {
      return `Variables exceed the ${DYNAMIC_VARS_MAX_BYTES}-byte limit.`;
    }
    return null;
  }

  function handleSubmit() {
    const err = validate();
    setLocalError(err);
    if (err) return;
    const vars = rowsToRecord(rows);

    if (isEdit && schedule) {
      const body: UpdateScheduleRequest = {
        enabled,
        window_start_local: start,
        window_end_local: end,
        days_of_week: days,
        dynamic_vars: vars,
        profile_override: override || null,
      };
      update.mutate({ id: schedule.id, body }, { onSuccess: onClose });
      return;
    }
    const body: CreateScheduleRequest = {
      contact_id: contactId,
      slot,
      window_start_local: start,
      window_end_local: end,
      days_of_week: days,
      enabled,
      dynamic_vars: vars,
      profile_override: override || null,
    };
    create.mutate(body, { onSuccess: onClose });
  }

  return (
    <Dialog open onClose={onClose} title={isEdit ? "Edit schedule" : "New schedule"}>
      <div className="space-y-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="sf-slot">
            Slot
          </label>
          {isEdit ? (
            <p className="text-sm text-ink">
              {slot} (slot is fixed; delete and recreate to change)
            </p>
          ) : (
            <Select
              id="sf-slot"
              aria-label="Slot"
              value={slot}
              onChange={(e) => setSlot(e.target.value as ScheduleSlot)}
            >
              {freeSlots.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </Select>
          )}
        </div>
        <div className="flex gap-3">
          <div className="flex-1">
            <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="sf-start">
              Window start
            </label>
            <Input
              id="sf-start"
              type="time"
              aria-label="Window start"
              value={start}
              onChange={(e) => setStart(e.target.value)}
            />
          </div>
          <div className="flex-1">
            <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="sf-end">
              Window end
            </label>
            <Input
              id="sf-end"
              type="time"
              aria-label="Window end"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
            />
          </div>
        </div>
        <div>
          <div className="mb-1 block text-xs font-medium text-slate-600">Days</div>
          <DaysOfWeekPicker value={days} onChange={setDays} />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="sf-override">
            Profile override (optional)
          </label>
          <Select id="sf-override" value={override} onChange={(e) => setOverride(e.target.value)}>
            <option value="">— use assigned/default —</option>
            {live.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </Select>
        </div>
        <KeyValueEditor rows={rows} onChange={setRows} label="Schedule variables" />
        <label className="flex items-center gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            aria-label="enabled"
          />
          Enabled
        </label>
        {localError ? <p className="text-xs font-medium text-red-700">{localError}</p> : null}
        {serverError ? <p className="text-xs font-medium text-red-700">{serverError}</p> : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="button" onClick={handleSubmit} disabled={busy}>
            {isEdit ? (busy ? "Saving…" : "Save") : busy ? "Creating…" : "Create"}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
