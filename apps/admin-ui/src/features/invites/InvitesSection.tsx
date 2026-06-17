import { useState, type FormEvent } from "react";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Input } from "../../components/ui/input";
import { Select } from "../../components/ui/select";
import { Button } from "../../components/ui/button";
import { pushToast } from "../../components/ui/toast";
import type { AdminUserRole } from "../../types/api";
import { useCreateInvite, useInvites, useResendInvite, useRevokeInvite } from "./hooks";

async function copy(url: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(url);
    pushToast("Invite link copied", "info");
  } catch {
    pushToast("Copy failed — select and copy the link manually");
  }
}

export function InvitesSection() {
  const invites = useInvites();
  const create = useCreateInvite();
  const revoke = useRevokeInvite();
  const resend = useResendInvite();

  const [email, setEmail] = useState("");
  const [role, setRole] = useState<AdminUserRole>("admin");

  function handleInvite(e: FormEvent): void {
    e.preventDefault();
    const trimmed = email.trim().toLowerCase();
    if (trimmed.length === 0) return;
    create.mutate(
      { email: trimmed, role },
      {
        onSuccess: (inv) => {
          setEmail("");
          setRole("admin");
          void copy(inv.accept_url);
        },
      },
    );
  }

  const list = invites.data ?? [];

  return (
    <div className="space-y-3">
      <h2 className="font-display text-lg text-ink-strong">Pending invites</h2>
      <form
        onSubmit={handleInvite}
        className="flex flex-wrap items-end gap-3 rounded-xl border border-line bg-surface p-4 shadow-card"
      >
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="i-email">
            Email
          </label>
          <Input
            id="i-email"
            type="email"
            className="w-72"
            placeholder="person@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="i-role">
            Role
          </label>
          <Select
            id="i-role"
            className="w-40"
            value={role}
            onChange={(e) => setRole(e.target.value as AdminUserRole)}
          >
            <option value="admin">admin</option>
            <option value="viewer">viewer</option>
          </Select>
        </div>
        <Button type="submit" disabled={create.isPending || email.trim().length === 0}>
          {create.isPending ? "Inviting…" : "Invite"}
        </Button>
      </form>

      <Table>
        <Thead>
          <Tr>
            <Th>Email</Th>
            <Th>Role</Th>
            <Th>Invited by</Th>
            <Th>Expires</Th>
            <Th className="text-right">Actions</Th>
          </Tr>
        </Thead>
        <Tbody>
          {list.length === 0 ? (
            <Tr>
              <Td className="text-slate-500" colSpan={5}>
                No pending invites.
              </Td>
            </Tr>
          ) : null}
          {list.map((inv) => (
            <Tr key={inv.id}>
              <Td className="font-medium">{inv.email}</Td>
              <Td>{inv.role}</Td>
              <Td className="text-xs text-slate-500">{inv.invited_by ?? "—"}</Td>
              <Td className="text-xs text-slate-500">
                {new Date(inv.expires_at).toLocaleString()}
              </Td>
              <Td className="space-x-2 text-right">
                <Button variant="secondary" onClick={() => void copy(inv.accept_url)}>
                  Copy link
                </Button>
                <Button
                  variant="secondary"
                  disabled={resend.isPending}
                  onClick={() => resend.mutate(inv.id)}
                >
                  Resend
                </Button>
                <Button
                  variant="danger"
                  disabled={revoke.isPending}
                  onClick={() => revoke.mutate(inv.id)}
                >
                  Revoke
                </Button>
              </Td>
            </Tr>
          ))}
        </Tbody>
      </Table>
    </div>
  );
}
