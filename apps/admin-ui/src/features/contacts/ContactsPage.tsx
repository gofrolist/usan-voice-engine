import { useState } from "react";
import { Link } from "react-router-dom";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Select } from "../../components/ui/select";
import { Spinner } from "../../components/ui/spinner";
import { Button } from "../../components/ui/button";
import { useIsAdmin } from "../../auth/useSession";
import { useProfiles } from "../profiles/hooks";
import { US_TIMEZONES } from "./timezones";
import { useAssignProfile, useContacts, useSetTimezone } from "./hooks";
import { ContactFormDialog } from "./ContactFormDialog";

const PAGE_SIZE = 200;

// Admin-only: assign each contact a specific agent profile, or "— none —" to fall
// back to the per-direction default. Only active profiles are assignable.
export function ContactsPage() {
  const isAdmin = useIsAdmin();
  const [offset, setOffset] = useState(0);
  const [createOpen, setCreateOpen] = useState(false);
  const contacts = useContacts(PAGE_SIZE, offset);
  const profiles = useProfiles();
  const assign = useAssignProfile();
  const setTz = useSetTimezone();

  if (!isAdmin) {
    return <p className="text-sm text-muted">Admins only.</p>;
  }

  if (contacts.isLoading || profiles.isLoading) {
    return (
      <div className="flex items-center gap-2 text-muted">
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
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl text-ink-strong">Contacts</h1>
        <Button onClick={() => setCreateOpen(true)}>+ New contact</Button>
      </div>
      <Table>
        <Thead>
          <Tr>
            <Th>Name</Th>
            <Th>Phone</Th>
            <Th>Timezone</Th>
            <Th>Assigned profile</Th>
          </Tr>
        </Thead>
        <Tbody>
          {contactList.length === 0 ? (
            <Tr>
              <Td className="text-faint" colSpan={4}>
                No contacts.
              </Td>
            </Tr>
          ) : null}
          {contactList.map((e) => (
            <Tr key={e.id}>
              <Td className="font-medium text-slate-900">
                <Link className="text-accent hover:underline" to={`/contacts/${e.id}`}>
                  {e.name}
                </Link>
              </Td>
              <Td className="font-mono text-xs">{e.masked_phone}</Td>
              <Td>
                <Select
                  aria-label={`Timezone for ${e.name}`}
                  value={e.timezone}
                  disabled={setTz.isPending && setTz.variables?.contactId === e.id}
                  onChange={(ev) =>
                    setTz.mutate({ contactId: e.id, timezone: ev.target.value })
                  }
                >
                  {/* Keep a current value that isn't in the curated US set visible. */}
                  {!US_TIMEZONES.some((tz) => tz.value === e.timezone) ? (
                    <option value={e.timezone}>{e.timezone}</option>
                  ) : null}
                  {US_TIMEZONES.map((tz) => (
                    <option key={tz.value} value={tz.value}>
                      {tz.label}
                    </option>
                  ))}
                </Select>
              </Td>
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

      {createOpen ? (
        <ContactFormDialog mode="create" onClose={() => setCreateOpen(false)} />
      ) : null}

      <div className="flex items-center gap-3 text-sm text-muted">
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
