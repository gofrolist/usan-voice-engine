import { useState, type FormEvent } from "react";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Input } from "../../components/ui/input";
import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";
import { Spinner } from "../../components/ui/spinner";
import { Dialog } from "../../components/ui/dialog";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { useIsAdmin } from "../../auth/useSession";
import { useVariableCatalog } from "../../config/variableCatalog";
import { DeclareVariableDialog, FieldLabel } from "./DeclareVariableDialog";
import {
  useCreateCustomVariable,
  useCustomVariableReferences,
  useCustomVariables,
  useDeleteCustomVariable,
  useUpdateCustomVariable,
  type CustomVariable,
  type CustomVariableUpdate,
} from "./hooks";

function EditVariableDialog({
  variable,
  busy,
  onCancel,
  onSave,
}: {
  variable: CustomVariable;
  busy: boolean;
  onCancel: () => void;
  onSave: (body: CustomVariableUpdate) => void;
}) {
  // name is immutable after create — shown in the title, never as an input.
  const [description, setDescription] = useState(variable.description);
  const [example, setExample] = useState(variable.example);
  const [phi, setPhi] = useState(variable.phi);

  function handleSubmit(e: FormEvent): void {
    e.preventDefault();
    onSave({ description, example, phi });
  }

  return (
    <Dialog open onClose={onCancel} title={`Edit {{${variable.name}}}`}>
      <form onSubmit={handleSubmit} className="space-y-3">
        <div>
          <FieldLabel htmlFor="cv-edit-description">Description</FieldLabel>
          <Input
            id="cv-edit-description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>
        <div>
          <FieldLabel htmlFor="cv-edit-example">Example</FieldLabel>
          <Input
            id="cv-edit-example"
            value={example}
            onChange={(e) => setExample(e.target.value)}
          />
        </div>
        <label className="flex items-center gap-2 text-sm text-slate-700">
          <input type="checkbox" checked={phi} onChange={(e) => setPhi(e.target.checked)} />
          PHI — per-call value will contain health information
        </label>
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy}>
            {busy ? "Saving…" : "Save"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

// Catalog CRUD for the "custom" variable tier. The list is readable by every
// session role; mutations are ADMIN-only (server-enforced, mirrored here by
// hiding the buttons via useIsAdmin — the AdminUsersPage idiom).
export function CustomVariablesPage() {
  const isAdmin = useIsAdmin();
  const variables = useCustomVariables();
  const catalog = useVariableCatalog();
  const create = useCreateCustomVariable();
  const update = useUpdateCustomVariable();
  const remove = useDeleteCustomVariable();

  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<CustomVariable | null>(null);
  const [toDelete, setToDelete] = useState<CustomVariable | null>(null);

  // Builtin names for the create dialog's client-side collision mirror (FR-006).
  const builtinNames = new Set(
    (catalog.data ?? []).filter((v) => v.tier === "builtin").map((v) => v.name),
  );
  // Delete-guard (FR-007): fetch where the queued variable is referenced so the
  // confirm dialog can list the profiles/locations before deleting.
  const references = useCustomVariableReferences(toDelete?.id ?? null);

  if (variables.isLoading) {
    return (
      <div className="flex items-center gap-2 text-slate-600">
        <Spinner /> Loading custom variables…
      </div>
    );
  }
  if (variables.isError) {
    return (
      <p className="text-sm text-red-700">
        Failed to load custom variables: {(variables.error as Error)?.message}
      </p>
    );
  }

  const list = variables.data ?? [];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Custom variables</h1>
        {isAdmin ? <Button onClick={() => setCreateOpen(true)}>New variable</Button> : null}
      </div>
      <p className="text-sm text-slate-600">
        Custom variables document the <code className="font-mono text-xs">{"{{tokens}}"}</code>{" "}
        operator systems supply per call via dynamic variables. Definitions carry no values.
      </p>

      <Table>
        <Thead>
          <Tr>
            <Th>Name</Th>
            <Th>Description</Th>
            <Th>Example</Th>
            <Th>PHI</Th>
            {isAdmin ? <Th className="text-right">Actions</Th> : null}
          </Tr>
        </Thead>
        <Tbody>
          {list.length === 0 ? (
            <Tr>
              <Td className="text-slate-500" colSpan={isAdmin ? 5 : 4}>
                No custom variables.
              </Td>
            </Tr>
          ) : null}
          {list.map((v) => (
            <Tr key={v.id}>
              <Td className="font-mono text-xs text-indigo-700">{v.name}</Td>
              <Td>{v.description || "—"}</Td>
              <Td className="text-slate-500">{v.example || "—"}</Td>
              <Td>{v.phi ? <Badge tone="red">PHI</Badge> : <span className="text-slate-400">—</span>}</Td>
              {isAdmin ? (
                <Td className="text-right">
                  <span className="inline-flex gap-2">
                    <Button variant="secondary" onClick={() => setEditing(v)}>
                      Edit
                    </Button>
                    <Button variant="danger" onClick={() => setToDelete(v)}>
                      Delete
                    </Button>
                  </span>
                </Td>
              ) : null}
            </Tr>
          ))}
        </Tbody>
      </Table>

      {createOpen ? (
        <DeclareVariableDialog
          busy={create.isPending}
          builtinNames={builtinNames}
          onCancel={() => setCreateOpen(false)}
          onCreate={(body) => create.mutate(body, { onSuccess: () => setCreateOpen(false) })}
        />
      ) : null}
      {editing ? (
        <EditVariableDialog
          variable={editing}
          busy={update.isPending}
          onCancel={() => setEditing(null)}
          onSave={(body) =>
            update.mutate({ id: editing.id, body }, { onSuccess: () => setEditing(null) })
          }
        />
      ) : null}
      <ConfirmDialog
        open={toDelete !== null}
        title="Delete custom variable?"
        body={
          <div className="space-y-2">
            <p>
              Delete <strong>{toDelete?.name}</strong>? Templates referencing{" "}
              <code className="font-mono text-xs">{`{{${toDelete?.name}}}`}</code> keep working but
              revert to unknown-variable warnings on the next save.
            </p>
            {references.isLoading ? (
              <p className="text-xs text-slate-500">Checking where it&apos;s used…</p>
            ) : references.data && references.data.profiles.length > 0 ? (
              <div className="rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900">
                <p className="font-medium">
                  Still referenced by {references.data.profiles.length}{" "}
                  {references.data.profiles.length === 1 ? "profile" : "profiles"}:
                </p>
                <ul className="mt-1 list-disc pl-4">
                  {references.data.profiles.map((p) => (
                    <li key={p.id}>
                      <span className="font-medium">{p.name}</span> ({p.where.join(", ")})
                    </li>
                  ))}
                </ul>
              </div>
            ) : references.data ? (
              <p className="text-xs text-slate-500">Not referenced by any profile.</p>
            ) : null}
          </div>
        }
        confirmLabel="Delete"
        busy={remove.isPending}
        onCancel={() => setToDelete(null)}
        onConfirm={() => {
          if (!toDelete) return;
          remove.mutate(toDelete.id, { onSuccess: () => setToDelete(null) });
        }}
      />
    </div>
  );
}
