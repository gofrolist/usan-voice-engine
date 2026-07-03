import { useState, type FormEvent } from "react";
import { Dialog } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Button } from "../../components/ui/button";
import type { ApiError } from "../../lib/api";
import { useCreateKb } from "./hooks";

interface Props {
  onClose: () => void;
  onCreated: (id: string) => void;
}

// Hand-rolled useState form (codebase convention — no RHF). Name only for v1.
export function KnowledgeBaseFormDialog({ onClose, onCreated }: Props) {
  const [name, setName] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const create = useCreateKb();
  const serverError = (create.error as ApiError | null)?.detail;

  function handleSubmit(e: FormEvent): void {
    e.preventDefault();
    setLocalError(null);
    const trimmed = name.trim();
    if (trimmed.length === 0) {
      setLocalError("Name is required.");
      return;
    }
    create.mutate({ name: trimmed }, { onSuccess: (kb) => onCreated(kb.id) });
  }

  return (
    <Dialog open onClose={onClose} title="New knowledge base">
      <form onSubmit={handleSubmit} className="space-y-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="kb-name">
            Name
          </label>
          <Input id="kb-name" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        {localError ? <p className="text-xs font-medium text-red-700">{localError}</p> : null}
        {serverError ? <p className="text-xs font-medium text-red-700">{serverError}</p> : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={create.isPending}>
            Cancel
          </Button>
          <Button type="submit" disabled={create.isPending}>
            {create.isPending ? "Creating…" : "Create"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
