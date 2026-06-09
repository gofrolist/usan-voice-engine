import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Spinner } from "../../components/ui/spinner";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { useIsAdmin } from "../../auth/useSession";
import type { ProfileSummary } from "../../types/api";
import { useArchiveProfile, useProfiles } from "./hooks";
import { NewProfileDialog } from "./NewProfileDialog";

// Profiles overview: status + default badges, live version, draft indicator,
// assigned-elder count. Row click opens the editor. Admin-only actions are gated.
export function ProfilesListPage() {
  const navigate = useNavigate();
  const isAdmin = useIsAdmin();
  const { data: profiles, isLoading, isError, error } = useProfiles();
  const archive = useArchiveProfile();

  const [newOpen, setNewOpen] = useState(false);
  const [cloneFrom, setCloneFrom] = useState<string>("");
  const [toArchive, setToArchive] = useState<ProfileSummary | null>(null);

  function openNew(prefillClone = ""): void {
    setCloneFrom(prefillClone);
    setNewOpen(true);
  }

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 text-slate-600">
        <Spinner /> Loading profiles…
      </div>
    );
  }

  if (isError) {
    return <p className="text-sm text-red-700">Failed to load profiles: {(error as Error)?.message}</p>;
  }

  const list = profiles ?? [];
  const active = list.filter((p) => p.status === "active");

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Agent profiles</h1>
        {isAdmin ? <Button onClick={() => openNew()}>New profile</Button> : null}
      </div>

      <Table>
        <Thead>
          <Tr>
            <Th>Name</Th>
            <Th>Status</Th>
            <Th>Defaults</Th>
            <Th>Live version</Th>
            <Th>Draft</Th>
            <Th>Elders</Th>
            <Th className="text-right">Actions</Th>
          </Tr>
        </Thead>
        <Tbody>
          {list.length === 0 ? (
            <Tr>
              <Td className="text-slate-500" colSpan={7}>
                No profiles yet.
              </Td>
            </Tr>
          ) : null}
          {list.map((p) => (
            <Tr
              key={p.id}
              className="cursor-pointer"
              onClick={() => navigate(`/profiles/${p.id}`)}
            >
              <Td className="font-medium text-slate-900">
                {p.name}
                {p.description ? (
                  <div className="text-xs font-normal text-slate-500">{p.description}</div>
                ) : null}
              </Td>
              <Td>
                <Badge tone={p.status === "active" ? "green" : "gray"}>{p.status}</Badge>
              </Td>
              <Td>
                <div className="flex gap-1">
                  {p.is_default_inbound ? <Badge tone="blue">in</Badge> : null}
                  {p.is_default_outbound ? <Badge tone="blue">out</Badge> : null}
                  {!p.is_default_inbound && !p.is_default_outbound ? (
                    <span className="text-slate-400">—</span>
                  ) : null}
                </div>
              </Td>
              <Td>{p.published_version ?? <span className="text-slate-400">none</span>}</Td>
              <Td>
                {p.has_unpublished_draft ? (
                  <span title="Unpublished draft changes">
                    <Badge tone="amber">unpublished</Badge>
                  </span>
                ) : (
                  <span className="text-slate-400">—</span>
                )}
              </Td>
              <Td>{p.assigned_elder_count}</Td>
              <Td className="text-right" onClick={(e) => e.stopPropagation()}>
                <div className="flex justify-end gap-2">
                  <Button
                    variant="ghost"
                    onClick={() => navigate(`/profiles/${p.id}/versions`)}
                  >
                    History
                  </Button>
                  {isAdmin ? (
                    <>
                      <Button variant="secondary" onClick={() => openNew(p.id)}>
                        Clone
                      </Button>
                      {p.status === "active" ? (
                        <Button variant="danger" onClick={() => setToArchive(p)}>
                          Archive
                        </Button>
                      ) : null}
                    </>
                  ) : null}
                </div>
              </Td>
            </Tr>
          ))}
        </Tbody>
      </Table>

      <NewProfileDialog
        open={newOpen}
        onClose={() => setNewOpen(false)}
        cloneSources={active}
        key={cloneFrom + String(newOpen)}
        initialCloneFrom={cloneFrom}
      />

      <ConfirmDialog
        open={toArchive !== null}
        title="Archive profile?"
        body={
          <>
            Archive <strong>{toArchive?.name}</strong>? Archived profiles can no longer be set as a
            default and stop appearing in elder assignment. A profile that is still a default or
            assigned to elders cannot be archived (the server will reject it).
          </>
        }
        confirmLabel="Archive"
        busy={archive.isPending}
        onCancel={() => setToArchive(null)}
        onConfirm={() => {
          if (!toArchive) return;
          archive.mutate(toArchive.id, { onSuccess: () => setToArchive(null) });
        }}
      />
    </div>
  );
}
