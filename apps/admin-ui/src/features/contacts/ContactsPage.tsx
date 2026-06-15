import { useState } from "react";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Select } from "../../components/ui/select";
import { Spinner } from "../../components/ui/spinner";
import { Button } from "../../components/ui/button";
import { useIsAdmin } from "../../auth/useSession";
import { useProfiles } from "../profiles/hooks";
import { useAssignProfile, useContacts } from "./hooks";

const PAGE_SIZE = 200;

// Admin-only: assign each contact a specific agent profile, or "— none —" to fall
// back to the per-direction default. Only active profiles are assignable.
export function ContactsPage() {
  const isAdmin = useIsAdmin();
  const [offset, setOffset] = useState(0);
  const contacts = useContacts(PAGE_SIZE, offset);
  const profiles = useProfiles();
  const assign = useAssignProfile();

  if (!isAdmin) {
    return <p className="text-sm text-slate-600">Admins only.</p>;
  }

  if (contacts.isLoading || profiles.isLoading) {
    return (
      <div className="flex items-center gap-2 text-slate-600">
        <Spinner /> Loading contacts…
      </div>
    );
  }
  if (contacts.isError) {
    return (
      <p className="text-sm text-red-700">
        Failed to load contacts: {(contacts.error as Error)?.message}
      </p>
    );
  }

  const contactList = contacts.data ?? [];
  const assignable = (profiles.data ?? []).filter((p) => p.status === "active");
  // A full page means there may be more rows; a short page is the last one.
  const hasNext = contactList.length === PAGE_SIZE;
  const hasPrev = offset > 0;
  const rangeStart = contactList.length === 0 ? 0 : offset + 1;
  const rangeEnd = offset + contactList.length;

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Contacts</h1>
      <Table>
        <Thead>
          <Tr>
            <Th>Name</Th>
            <Th>Phone</Th>
            <Th>Assigned profile</Th>
          </Tr>
        </Thead>
        <Tbody>
          {contactList.length === 0 ? (
            <Tr>
              <Td className="text-slate-500" colSpan={3}>
                No contacts.
              </Td>
            </Tr>
          ) : null}
          {contactList.map((e) => (
            <Tr key={e.id}>
              <Td className="font-medium text-slate-900">{e.name}</Td>
              <Td className="font-mono text-xs">{e.masked_phone}</Td>
              <Td>
                <Select
                  aria-label={`Assigned profile for ${e.name}`}
                  value={e.agent_profile_id ?? ""}
                  // Disable only the row being saved, not the whole table.
                  disabled={assign.isPending && assign.variables?.contactId === e.id}
                  onChange={(ev) =>
                    assign.mutate({
                      contactId: e.id,
                      agentProfileId: ev.target.value === "" ? null : ev.target.value,
                    })
                  }
                >
                  <option value="">— none —</option>
                  {/* Keep a currently-assigned-but-archived profile visible. */}
                  {e.agent_profile_id &&
                  !assignable.some((p) => p.id === e.agent_profile_id) ? (
                    <option value={e.agent_profile_id}>
                      {e.agent_profile_name ?? e.agent_profile_id} (archived)
                    </option>
                  ) : null}
                  {assignable.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </Select>
              </Td>
            </Tr>
          ))}
        </Tbody>
      </Table>

      <div className="flex items-center gap-3 text-sm text-slate-600">
        <Button
          variant="secondary"
          disabled={!hasPrev}
          onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
        >
          Previous
        </Button>
        <span>
          {rangeStart}–{rangeEnd}
        </span>
        <Button variant="secondary" disabled={!hasNext} onClick={() => setOffset(offset + PAGE_SIZE)}>
          Next
        </Button>
      </div>
    </div>
  );
}
