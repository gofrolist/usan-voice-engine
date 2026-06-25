import { useState, type FormEvent } from "react";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Input } from "../../components/ui/input";
import { Button } from "../../components/ui/button";
import { Spinner } from "../../components/ui/spinner";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { useSession } from "../../auth/useSession";
import { useCompatKeys, useCreateCompatKey, useRevokeCompatKey } from "./hooks";
import { CreatedKeyDialog } from "./CreatedKeyDialog";
import type { CompatKey, CompatKeyCreated } from "../../types/api";

// Super-admin screen to mint, list, and revoke RetellAI-compat API keys. A key is how a
// RetellAI client authenticates against our compat surface; it is scoped to the ACTIVE org
// (the super-admin "acts as" the target org first). The server enforces super-admin (403)
// and org scope; the list query stays disabled for non-super-admins.
function formatTs(iso: string | null): string {
  return iso === null ? "—" : new Date(iso).toLocaleString();
}

export function CompatKeysPage() {
  const { data: me } = useSession();
  const isSuperAdmin = !!me?.is_super_admin;
  const keys = useCompatKeys(isSuperAdmin);
  const create = useCreateCompatKey();
  const revoke = useRevokeCompatKey();

  const [label, setLabel] = useState("");
  const [created, setCreated] = useState<CompatKeyCreated | null>(null);
  const [toRevoke, setToRevoke] = useState<CompatKey | null>(null);

  if (!isSuperAdmin) {
    return <p className="text-sm text-slate-600">Super-admins only.</p>;
  }

  function handleCreate(e: FormEvent): void {
    e.preventDefault();
    const trimmed = label.trim();
    create.mutate(
      { label: trimmed.length > 0 ? trimmed : null },
      {
        onSuccess: (createdKey) => {
          setLabel("");
          setCreated(createdKey);
        },
      },
    );
  }

  if (keys.isLoading) {
    return (
      <div className="flex items-center gap-2 text-slate-600">
        <Spinner /> Loading API keys…
      </div>
    );
  }
  if (keys.isError) {
    return (
      <p className="text-sm text-red-700">
        Failed to load API keys: {(keys.error as Error)?.message}
      </p>
    );
  }

  const list = keys.data ?? [];

  return (
    <div className="space-y-4">
      <h1 className="font-display text-2xl text-ink-strong">Compat API Keys</h1>
      <p className="text-sm text-slate-600">
        Keys authenticate RetellAI-compatible clients against{" "}
        <span className="font-medium">{me?.active_org?.name ?? "the active organization"}</span>.
        The token is shown once at creation.
      </p>

      <form
        onSubmit={handleCreate}
        className="flex flex-wrap items-end gap-3 rounded-xl border border-line bg-surface p-4 shadow-card"
      >
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="k-label">
            Label (optional)
          </label>
          <Input
            id="k-label"
            className="w-72"
            placeholder="e.g. Acme CRM production"
            maxLength={200}
            value={label}
            onChange={(e) => setLabel(e.target.value)}
          />
        </div>
        <Button type="submit" disabled={create.isPending}>
          {create.isPending ? "Creating…" : "Create key"}
        </Button>
      </form>

      <Table>
        <Thead>
          <Tr>
            <Th>Key</Th>
            <Th>Label</Th>
            <Th>Status</Th>
            <Th>Created</Th>
            <Th>Last used</Th>
            <Th className="text-right">Actions</Th>
          </Tr>
        </Thead>
        <Tbody>
          {list.length === 0 ? (
            <Tr>
              <Td className="text-slate-500" colSpan={6}>
                No API keys.
              </Td>
            </Tr>
          ) : null}
          {list.map((k) => (
            <Tr key={k.id}>
              <Td className="font-mono text-xs">{k.key_prefix}…</Td>
              <Td className="text-slate-600">{k.label ?? "—"}</Td>
              <Td className="text-xs uppercase tracking-wide text-slate-500">{k.status}</Td>
              <Td className="text-slate-500">{formatTs(k.created_at)}</Td>
              <Td className="text-slate-500">{formatTs(k.last_used_at)}</Td>
              <Td className="text-right">
                {k.status === "active" ? (
                  <Button
                    variant="danger"
                    aria-label={`Revoke ${k.key_prefix}`}
                    disabled={revoke.isPending}
                    onClick={() => setToRevoke(k)}
                  >
                    Revoke
                  </Button>
                ) : null}
              </Td>
            </Tr>
          ))}
        </Tbody>
      </Table>

      <CreatedKeyDialog created={created} onDone={() => setCreated(null)} />

      <ConfirmDialog
        open={toRevoke !== null}
        title="Revoke API key?"
        body={
          <>
            Any client using <code className="font-mono">{toRevoke?.key_prefix}…</code> will
            immediately lose access. This cannot be undone.
          </>
        }
        confirmLabel="Revoke"
        busy={revoke.isPending}
        onConfirm={() => {
          if (toRevoke !== null) {
            revoke.mutate(toRevoke.id, { onSuccess: () => setToRevoke(null) });
          }
        }}
        onCancel={() => setToRevoke(null)}
      />
    </div>
  );
}
