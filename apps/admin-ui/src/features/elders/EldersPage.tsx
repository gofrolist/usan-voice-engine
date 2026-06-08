import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Select } from "../../components/ui/select";
import { Spinner } from "../../components/ui/spinner";
import { useIsAdmin } from "../../auth/useSession";
import { useProfiles } from "../profiles/hooks";
import { useAssignProfile, useElders } from "./hooks";

// Admin-only: assign each elder a specific agent profile, or "— none —" to fall
// back to the per-direction default. Only active profiles are assignable.
export function EldersPage() {
  const isAdmin = useIsAdmin();
  const elders = useElders();
  const profiles = useProfiles();
  const assign = useAssignProfile();

  if (!isAdmin) {
    return <p className="text-sm text-gray-600">Admins only.</p>;
  }

  if (elders.isLoading || profiles.isLoading) {
    return (
      <div className="flex items-center gap-2 text-gray-600">
        <Spinner /> Loading elders…
      </div>
    );
  }
  if (elders.isError) {
    return (
      <p className="text-sm text-red-700">
        Failed to load elders: {(elders.error as Error)?.message}
      </p>
    );
  }

  const elderList = elders.data ?? [];
  const assignable = (profiles.data ?? []).filter((p) => p.status === "active");

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Elders</h1>
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
              <Td className="text-gray-500" colSpan={3}>
                No elders.
              </Td>
            </Tr>
          ) : null}
          {elderList.map((e) => (
            <Tr key={e.id}>
              <Td className="font-medium text-gray-900">{e.name}</Td>
              <Td className="font-mono text-xs">{e.masked_phone}</Td>
              <Td>
                <Select
                  aria-label={`Assigned profile for ${e.name}`}
                  value={e.agent_profile_id ?? ""}
                  disabled={assign.isPending}
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
    </div>
  );
}
