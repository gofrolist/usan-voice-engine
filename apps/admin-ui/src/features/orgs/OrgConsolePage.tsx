import { useState, type FormEvent } from "react";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Input } from "../../components/ui/input";
import { Button } from "../../components/ui/button";
import { Spinner } from "../../components/ui/spinner";
import { useSession } from "../../auth/useSession";
import { useCreateOrg, useOrganizations, useSwitchOrg } from "./hooks";

// Super-admin org console. Lists every organization, creates new ones (optionally
// seeding a first admin), and lets the super-admin "act as" any org. The whole
// screen is super-admin-only; the server enforces it (403) and the org list query
// stays disabled for everyone else.
export function OrgConsolePage() {
  const { data: me } = useSession();
  const isSuperAdmin = !!me?.is_super_admin;
  const orgs = useOrganizations(isSuperAdmin);
  const create = useCreateOrg();
  const switchOrg = useSwitchOrg();

  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [firstAdmin, setFirstAdmin] = useState("");

  if (!isSuperAdmin) {
    return <p className="text-sm text-slate-600">Super-admins only.</p>;
  }

  function handleCreate(e: FormEvent): void {
    e.preventDefault();
    const trimmedName = name.trim();
    const trimmedSlug = slug.trim();
    if (trimmedName.length === 0 || trimmedSlug.length === 0) return;
    const email = firstAdmin.trim().toLowerCase();
    create.mutate(
      {
        name: trimmedName,
        slug: trimmedSlug,
        first_admin_email: email.length > 0 ? email : null,
      },
      {
        onSuccess: () => {
          setName("");
          setSlug("");
          setFirstAdmin("");
        },
      },
    );
  }

  if (orgs.isLoading) {
    return (
      <div className="flex items-center gap-2 text-slate-600">
        <Spinner /> Loading organizations…
      </div>
    );
  }
  if (orgs.isError) {
    return (
      <p className="text-sm text-red-700">
        Failed to load organizations: {(orgs.error as Error)?.message}
      </p>
    );
  }

  const list = orgs.data ?? [];
  const canCreate = name.trim().length > 0 && slug.trim().length > 0;

  return (
    <div className="space-y-4">
      <h1 className="font-display text-2xl text-ink-strong">Organizations</h1>

      <form
        onSubmit={handleCreate}
        className="flex flex-wrap items-end gap-3 rounded-xl border border-line bg-surface p-4 shadow-card"
      >
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="o-name">
            Name
          </label>
          <Input
            id="o-name"
            className="w-56"
            placeholder="Acme Care"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="o-slug">
            Slug
          </label>
          <Input
            id="o-slug"
            className="w-48"
            placeholder="acme-care"
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="o-admin">
            First admin email
          </label>
          <Input
            id="o-admin"
            type="email"
            className="w-64"
            placeholder="admin@example.com (optional)"
            value={firstAdmin}
            onChange={(e) => setFirstAdmin(e.target.value)}
          />
        </div>
        <Button type="submit" disabled={create.isPending || !canCreate}>
          {create.isPending ? "Creating…" : "Create"}
        </Button>
      </form>

      <Table>
        <Thead>
          <Tr>
            <Th>Name</Th>
            <Th>Slug</Th>
            <Th>Status</Th>
            <Th className="text-right">Actions</Th>
          </Tr>
        </Thead>
        <Tbody>
          {list.length === 0 ? (
            <Tr>
              <Td className="text-slate-500" colSpan={4}>
                No organizations.
              </Td>
            </Tr>
          ) : null}
          {list.map((o) => (
            <Tr key={o.id}>
              <Td className="font-medium">{o.name}</Td>
              <Td className="text-slate-500">{o.slug}</Td>
              <Td className="text-xs uppercase tracking-wide text-slate-500">{o.status}</Td>
              <Td className="text-right">
                <Button
                  variant="secondary"
                  aria-label={`Act as ${o.name}`}
                  disabled={switchOrg.isPending}
                  onClick={() => switchOrg.mutate({ organization_id: o.id })}
                >
                  Act as
                </Button>
              </Td>
            </Tr>
          ))}
        </Tbody>
      </Table>
    </div>
  );
}
