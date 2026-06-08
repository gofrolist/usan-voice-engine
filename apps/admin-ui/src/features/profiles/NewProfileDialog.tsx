import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Dialog } from "../../components/ui/dialog";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { Textarea } from "../../components/ui/textarea";
import { Select } from "../../components/ui/select";
import { useCreateProfile } from "./hooks";
import type { ProfileSummary } from "../../types/api";

interface NewProfileDialogProps {
  open: boolean;
  onClose: () => void;
  // Active profiles offered as clone sources.
  cloneSources: ProfileSummary[];
  // Preselected clone source id (the page remounts the dialog via `key`).
  initialCloneFrom?: string;
}

// Create a fresh profile or clone an existing one's published config. On success
// navigates straight into the editor for the new profile.
export function NewProfileDialog({
  open,
  onClose,
  cloneSources,
  initialCloneFrom = "",
}: NewProfileDialogProps) {
  const navigate = useNavigate();
  const create = useCreateProfile();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [cloneFrom, setCloneFrom] = useState(initialCloneFrom);

  function reset(): void {
    setName("");
    setDescription("");
    setCloneFrom("");
  }

  function handleClose(): void {
    if (create.isPending) return;
    reset();
    onClose();
  }

  function handleSubmit(e: FormEvent): void {
    e.preventDefault();
    const trimmed = name.trim();
    if (trimmed.length === 0) return;
    create.mutate(
      {
        name: trimmed,
        description: description.trim() || null,
        clone_from: cloneFrom || null,
      },
      {
        onSuccess: (profile) => {
          reset();
          onClose();
          navigate(`/profiles/${profile.id}`);
        },
      },
    );
  }

  return (
    <Dialog open={open} onClose={handleClose} title="New profile">
      <form onSubmit={handleSubmit} className="space-y-3">
        <div>
          <label className="mb-1 block text-sm font-medium text-gray-700" htmlFor="np-name">
            Name
          </label>
          <Input
            id="np-name"
            value={name}
            autoFocus
            placeholder="e.g. Daily Wellness — English"
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div>
          <label className="mb-1 block text-sm font-medium text-gray-700" htmlFor="np-desc">
            Description <span className="text-gray-400">(optional)</span>
          </label>
          <Textarea
            id="np-desc"
            rows={2}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>
        <div>
          <label className="mb-1 block text-sm font-medium text-gray-700" htmlFor="np-clone">
            Clone from <span className="text-gray-400">(optional)</span>
          </label>
          <Select id="np-clone" value={cloneFrom} onChange={(e) => setCloneFrom(e.target.value)}>
            <option value="">— Start from defaults —</option>
            {cloneSources.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </Select>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={handleClose} disabled={create.isPending}>
            Cancel
          </Button>
          <Button type="submit" disabled={create.isPending || name.trim().length === 0}>
            {create.isPending ? "Creating…" : "Create"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
