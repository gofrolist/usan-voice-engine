import { useState } from "react";
import { Dialog } from "../../components/ui/dialog";
import { Select } from "../../components/ui/select";
import { Button } from "../../components/ui/button";
import { KeyValueEditor, rowsToRecord, type KvRow } from "../../components/ui/KeyValueEditor";
import { DYNAMIC_VARS_MAX_BYTES, dynamicVarsByteSize } from "../../lib/dynamicVars";
import { pushToast } from "../../components/ui/toast";
import type { ApiError } from "../../lib/api";
import type { AdminCreateCallRequest, ContactDetail } from "../../types/api";
import { useProfiles } from "../profiles/hooks";
import { useCallNow } from "./hooks";

interface CallNowDialogProps {
  contact: ContactDetail;
  onClose: () => void;
}

// Ad-hoc outbound call. The required ack is a deliberate speed bump: the server
// does NOT enforce quiet-hours, so the operator confirms they mean to call outside
// the contact's window. DNC is a hard server block surfaced inline (not an error).
export function CallNowDialog({ contact, onClose }: CallNowDialogProps) {
  const [ack, setAck] = useState(false);
  const [override, setOverride] = useState("");
  const [rows, setRows] = useState<KvRow[]>([]);
  const [localError, setLocalError] = useState<string | null>(null);
  const [blocked, setBlocked] = useState(false);
  const profiles = useProfiles();
  const callNow = useCallNow();
  const serverError = (callNow.error as ApiError | null)?.detail ?? null;

  const live = (profiles.data ?? []).filter((p) => p.status === "active");

  function submit() {
    setLocalError(null);
    setBlocked(false);
    const vars = rowsToRecord(rows);
    if (dynamicVarsByteSize(vars) > DYNAMIC_VARS_MAX_BYTES) {
      setLocalError(`Variables exceed the ${DYNAMIC_VARS_MAX_BYTES}-byte limit.`);
      return;
    }
    const body: AdminCreateCallRequest = { contact_id: contact.id };
    if (Object.keys(vars).length > 0) body.dynamic_vars = vars;
    if (override) body.profile_override = override;
    callNow.mutate(body, {
      onSuccess: (call) => {
        if (call.status === "dnc_blocked") {
          setBlocked(true);
          return;
        }
        pushToast("Call queued.", "info");
        onClose();
      },
    });
  }

  return (
    <Dialog open onClose={onClose} title={`Call ${contact.name}`}>
      <div className="space-y-3">
        <p className="text-sm text-muted">
          Calling <span className="font-mono">{contact.masked_phone}</span> now.
        </p>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="cn-override">
            Profile override (optional)
          </label>
          <Select id="cn-override" value={override} onChange={(e) => setOverride(e.target.value)}>
            <option value="">— use assigned/default —</option>
            {live.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </Select>
        </div>
        <KeyValueEditor rows={rows} onChange={setRows} label="Call variables" />
        <label className="flex items-start gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={ack}
            onChange={(e) => setAck(e.target.checked)}
            aria-label="acknowledge outside their normal window"
          />
          I understand this calls the contact <strong>outside their normal window</strong>.
        </label>
        {blocked ? (
          <p className="text-sm font-medium text-red-700">
            This number is on the Do-Not-Call list; the call was blocked.
          </p>
        ) : null}
        {localError ? <p className="text-xs font-medium text-red-700">{localError}</p> : null}
        {serverError ? <p className="text-xs font-medium text-red-700">{serverError}</p> : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={callNow.isPending}>
            Cancel
          </Button>
          <Button type="button" onClick={submit} disabled={!ack || callNow.isPending}>
            {callNow.isPending ? "Calling…" : "Call now"}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
