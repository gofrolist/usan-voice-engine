import { useState, type FormEvent } from "react";
import { Dialog } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Button } from "../../components/ui/button";
import type { ApiError } from "../../lib/api";
import { useAddDnc } from "./hooks";

const E164 = /^\+[1-9]\d{7,14}$/;

export function AddDncDialog({ onClose }: { onClose: () => void }) {
  const [phone, setPhone] = useState("");
  const [reason, setReason] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const add = useAddDnc();
  const serverError = (add.error as ApiError | null)?.detail ?? null;

  function handleSubmit(e: FormEvent): void {
    e.preventDefault();
    setLocalError(null);
    if (!E164.test(phone.trim())) {
      setLocalError("Phone must be E.164 format, e.g. +19495551234.");
      return;
    }
    add.mutate({ phone_e164: phone.trim(), reason: reason.trim() || null }, { onSuccess: onClose });
  }

  return (
    <Dialog open onClose={onClose} title="Add to Do-Not-Call list">
      <form onSubmit={handleSubmit} className="space-y-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="dnc-phone">
            Phone (E.164)
          </label>
          <Input
            id="dnc-phone"
            placeholder="+19495551234"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="dnc-reason">
            Reason (optional)
          </label>
          <Input id="dnc-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
        </div>
        {localError ? <p className="text-xs font-medium text-red-700">{localError}</p> : null}
        {serverError ? <p className="text-xs font-medium text-red-700">{serverError}</p> : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={add.isPending}>
            Cancel
          </Button>
          <Button type="submit" disabled={add.isPending}>
            {add.isPending ? "Adding…" : "Add"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
