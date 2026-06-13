import { useState, type FormEvent } from "react";
import { Dialog } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Button } from "../../components/ui/button";
import type { CustomVariableCreate } from "./hooks";

// Spec §6.1 copy, asserted verbatim by the page test: definitions are operator
// configuration (no values), so the dialog warns against putting PHI in them.
export const PHI_HELP_TEXT =
  "Names, descriptions, and examples are operator configuration — never put PHI in " +
  "them. Mark a variable PHI if its per-call value will contain health information; " +
  "PHI variables are blocked in SMS templates.";

export function FieldLabel({ htmlFor, children }: { htmlFor: string; children: string }) {
  return (
    <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor={htmlFor}>
      {children}
    </label>
  );
}

interface DeclareVariableDialogProps {
  busy: boolean;
  onCancel: () => void;
  onCreate: (body: CustomVariableCreate) => void;
  // Inline declaration prefills the token name and renders it read-only (FR-002).
  // Omitted on the catalog page where the operator types a fresh name.
  name?: string;
  // Builtin names for the client-side collision mirror (FR-006). The server 422 is
  // authoritative; this just gives instant feedback before submit.
  builtinNames?: ReadonlySet<string>;
}

// Shared create dialog for the "custom" variable tier. Used both on the catalog page
// and inline from the prompt editor (name prefilled + read-only) so an operator can
// declare an undeclared {{token}} without leaving the editing screen (US1).
export function DeclareVariableDialog({
  busy,
  onCancel,
  onCreate,
  name: prefilledName,
  builtinNames,
}: DeclareVariableDialogProps) {
  const nameLocked = prefilledName !== undefined;
  const [name, setName] = useState(prefilledName ?? "");
  const [description, setDescription] = useState("");
  const [example, setExample] = useState("");
  const [phi, setPhi] = useState(false);

  const trimmed = name.trim();
  // Collision mirror: a typed name matching a builtin is blocked client-side (the
  // server still rejects it with 422 as the authority). A prefilled inline name is
  // by definition an unknown token, so it can never be a builtin.
  const collides = !nameLocked && trimmed.length > 0 && (builtinNames?.has(trimmed) ?? false);

  function handleSubmit(e: FormEvent): void {
    e.preventDefault();
    if (trimmed.length === 0 || collides) return;
    onCreate({ name: trimmed, description, example, phi });
  }

  return (
    <Dialog
      open
      onClose={onCancel}
      title={nameLocked ? `Declare {{${prefilledName}}}` : "New custom variable"}
    >
      <form onSubmit={handleSubmit} className="space-y-3">
        <p className="text-xs text-slate-500">{PHI_HELP_TEXT}</p>
        <div>
          <FieldLabel htmlFor="cv-name">Name</FieldLabel>
          <Input
            id="cv-name"
            placeholder="pet_name"
            value={name}
            readOnly={nameLocked}
            onChange={(e) => setName(e.target.value)}
          />
          {collides ? (
            <p className="mt-1 text-xs font-medium text-red-700">
              {`"${trimmed}" collides with a built-in variable — built-in names are reserved.`}
            </p>
          ) : (
            <p className="mt-1 text-xs text-slate-500">
              Lowercase snake_case; immutable after create (delete + recreate to rename).
            </p>
          )}
        </div>
        <div>
          <FieldLabel htmlFor="cv-description">Description</FieldLabel>
          <Input
            id="cv-description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>
        <div>
          <FieldLabel htmlFor="cv-example">Example</FieldLabel>
          <Input id="cv-example" value={example} onChange={(e) => setExample(e.target.value)} />
        </div>
        <label className="flex items-center gap-2 text-sm text-slate-700">
          <input type="checkbox" checked={phi} onChange={(e) => setPhi(e.target.checked)} />
          PHI — per-call value will contain health information
        </label>
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || trimmed.length === 0 || collides}>
            {busy ? "Creating…" : "Create"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
