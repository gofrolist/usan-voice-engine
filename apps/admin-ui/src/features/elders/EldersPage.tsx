import { useState } from "react";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Select } from "../../components/ui/select";
import { Spinner } from "../../components/ui/spinner";
import { Button } from "../../components/ui/button";
import { useIsAdmin } from "../../auth/useSession";
import { useProfiles } from "../profiles/hooks";
import { useAssignProfile, useElders } from "./hooks";

const PAGE_SIZE = 200;

// Admin-only: assign each contact a specific agent profile, or "— none —" to fall
// back to the per-direction default. Only active profiles are assignable.
export function EldersPage() {
  const isAdmin = useIsAdmin();
  const [offset, setOffset] = useState(0);
  const elders = useElders(PAGE_SIZE, offset);
  const profiles = useProfiles();
  const assign = useAssignProfile();

  if (!isAdmin) {
    return <p className="text-sm text-slate-600">Admins only.</p>;
  }

  if (elders.isLoading || profiles.isLoading) {
    return (
      <div className="flex items-center gap-2 text-slate-600">
        <Spinner /> Loading contacts…
      </div>
    );
  }
  if (elders.isError) {
    return (
      <p className="text-sm text-red-700">
        Failed to load contacts: {(elders.error as Error)?.message}
      </p>
    );
  }

  const elderList = elders.data ?? [];
  const assignable = (profiles.data ?? []).filter((p) => p.status === "active");
  // A full page means there may be more rows; a short page is the last one.
  const hasNext = elderList.length === PAGE_SIZE;
  const hasPrev = offset > 0;
  const rangeStart = elderList.length === 0 ? 0 : offset + 1;
  const rangeEnd = offset + elderList.length;

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
          {elderList.length === 0 ? (
            <Tr>
              <Td className="text-slate-500" colSpan={3}>
                No contacts.
              </Td>
            </Tr>
          ) : null}
          {elderList.map((e) => (
            <Tr key={e.id}>
              <Td className="font-medium text-slate-900">{e.name}</Td>
              <Td className="font-mono text-xs">{e.masked_phone}</Td>
              <Td>
                <Select
                  aria-label={`Assigned profile for ${e.name}`}
                  value={e.agent_profile_id ?? ""}
                  // Disable only the row being saved, not the whole table.
                  disabled={assign.isPending && assign.variables?.elderId === e.id}
                  onChange={(ev) =>
                    assign.mutate({
                      elderId: e.id,
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
