import { useState, type FormEvent } from "react";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Input } from "../../components/ui/input";
import { Select } from "../../components/ui/select";
import { Button } from "../../components/ui/button";
import { Spinner } from "../../components/ui/spinner";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { useIsAdmin } from "../../auth/useSession";
import type { AdminUserRole } from "../../types/api";
import { useAddMember, useMembers, useRemoveMember, useSetMemberRole } from "./hooks";
import { InvitesSection } from "../invites/InvitesSection";

// Members of the active org. Whole screen is admin-only; the server enforces the
// last-admin guard (409 on remove / on demoting the last admin) — we toast it.
export function MembersPage() {
  const isAdmin = useIsAdmin();
  const members = useMembers();
  const add = useAddMember();
  const setRole = useSetMemberRole();
  const remove = useRemoveMember();

  const [email, setEmail] = useState("");
  const [role, setRole_] = useState<AdminUserRole>("admin");
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
          setRole_("admin");
        },
      },
    );
  }

  if (members.isLoading) {
    return (
      <div className="flex items-center gap-2 text-slate-600">
        <Spinner /> Loading members…
      </div>
    );
  }
  if (members.isError) {
    return (
      <p className="text-sm text-red-700">
        Failed to load members: {(members.error as Error)?.message}
      </p>
    );
  }

  const list = members.data ?? [];

  return (
    <div className="space-y-4">
      <h1 className="font-display text-2xl text-ink-strong">Members</h1>

      <form
        onSubmit={handleAdd}
        className="flex flex-wrap items-end gap-3 rounded-xl border border-line bg-surface p-4 shadow-card"
      >
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="m-email">
            Email
          </label>
          <Input
            id="m-email"
            type="email"
            className="w-72"
            placeholder="person@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="m-role">
            Role
          </label>
          <Select
            id="m-role"
            className="w-40"
            value={role}
            onChange={(e) => setRole_(e.target.value as AdminUserRole)}
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
                No members.
              </Td>
            </Tr>
          ) : null}
          {list.map((m) => (
            <Tr key={m.email}>
              <Td className="font-medium">{m.email}</Td>
              <Td>
                <Select
                  className="w-32"
                  aria-label={`Role for ${m.email}`}
                  value={m.role}
                  disabled={setRole.isPending}
                  onChange={(e) =>
                    setRole.mutate({ email: m.email, role: e.target.value as AdminUserRole })
                  }
                >
                  <option value="admin">admin</option>
                  <option value="viewer">viewer</option>
                </Select>
              </Td>
              <Td className="text-xs text-slate-500">{m.added_by ?? "—"}</Td>
              <Td className="text-right">
                <Button variant="danger" onClick={() => setToRemove(m.email)}>
                  Remove
                </Button>
              </Td>
            </Tr>
          ))}
        </Tbody>
      </Table>

      <ConfirmDialog
        open={toRemove !== null}
        title="Remove member?"
        body={
          <>
            Remove <strong>{toRemove}</strong> from this organization? They will lose access on
            their next request. The server prevents removing the last admin.
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

      <InvitesSection />
    </div>
  );
}
