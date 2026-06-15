import { useState, type FormEvent } from "react";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Input } from "../../components/ui/input";
import { Select } from "../../components/ui/select";
import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";
import { Spinner } from "../../components/ui/spinner";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { useIsAdmin } from "../../auth/useSession";
import type { AdminUserRole } from "../../types/api";
import { useAddAdminUser, useAdminUsers, useRemoveAdminUser } from "./hooks";

// Whole screen is admin-only. The server enforces the last-admin guard (409 on
// remove); we surface that as a toast.
export function AdminUsersPage() {
  const isAdmin = useIsAdmin();
  const users = useAdminUsers();
  const add = useAddAdminUser();
  const remove = useRemoveAdminUser();

  const [email, setEmail] = useState("");
  const [role, setRole] = useState<AdminUserRole>("admin");
  const [toRemove, setToRemove] = useState<string | null>(null);

  if (!isAdmin) {
    return <p className="text-sm text-slate-600">Admins only.</p>;
  }

  function handleAdd(e: FormEvent): void {
    e.preventDefault();
    const trimmed = email.trim().toLowerCase();
    if (trimmed.length === 0) return;
    add.mutate(
      { email: trimmed, role },
      {
        onSuccess: () => {
          setEmail("");
          setRole("admin");
        },
      },
    );
  }

  if (users.isLoading) {
    return (
      <div className="flex items-center gap-2 text-slate-600">
        <Spinner /> Loading admin users…
      </div>
    );
  }
  if (users.isError) {
    return (
      <p className="text-sm text-red-700">
        Failed to load admin users: {(users.error as Error)?.message}
      </p>
    );
  }

  const list = users.data ?? [];

  return (
    <div className="space-y-4">
      <h1 className="font-display text-2xl text-ink-strong">Admin users (SSO allow-list)</h1>

      <form onSubmit={handleAdd} className="flex flex-wrap items-end gap-3 rounded-xl border border-line bg-surface p-4 shadow-card">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="au-email">
            Email
          </label>
          <Input
            id="au-email"
            type="email"
            className="w-72"
            placeholder="person@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="au-role">
            Role
          </label>
          <Select
            id="au-role"
            className="w-40"
            value={role}
            onChange={(e) => setRole(e.target.value as AdminUserRole)}
          >
            <option value="admin">admin</option>
            <option value="viewer">viewer</option>
          </Select>
        </div>
        <Button type="submit" disabled={add.isPending || email.trim().length === 0}>
          {add.isPending ? "Adding…" : "Add"}
        </Button>
      </form>

      <Table>
        <Thead>
          <Tr>
            <Th>Email</Th>
            <Th>Role</Th>
            <Th>Added by</Th>
            <Th className="text-right">Actions</Th>
          </Tr>
        </Thead>
        <Tbody>
          {list.length === 0 ? (
            <Tr>
              <Td className="text-slate-500" colSpan={4}>
                No admin users.
              </Td>
            </Tr>
          ) : null}
          {list.map((u) => (
            <Tr key={u.email}>
              <Td className="font-medium">{u.email}</Td>
              <Td>
                <Badge tone={u.role === "admin" ? "blue" : "gray"}>{u.role}</Badge>
              </Td>
              <Td className="text-xs text-slate-500">{u.added_by ?? "—"}</Td>
              <Td className="text-right">
                <Button variant="danger" onClick={() => setToRemove(u.email)}>
                  Remove
                </Button>
              </Td>
            </Tr>
          ))}
        </Tbody>
      </Table>

      <ConfirmDialog
        open={toRemove !== null}
        title="Remove admin user?"
        body={
          <>
            Remove <strong>{toRemove}</strong> from the allow-list? They will lose access on their
            next request. The server prevents removing the last admin.
          </>
        }
        confirmLabel="Remove"
        busy={remove.isPending}
        onCancel={() => setToRemove(null)}
        onConfirm={() => {
          if (!toRemove) return;
          remove.mutate(toRemove, { onSuccess: () => setToRemove(null) });
        }}
      />
    </div>
  );
}
