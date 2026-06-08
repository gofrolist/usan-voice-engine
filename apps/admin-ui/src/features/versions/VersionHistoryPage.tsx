import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";
import { Spinner } from "../../components/ui/spinner";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { DiffView } from "../../components/DiffView";
import { useIsAdmin } from "../../auth/useSession";
import { useVersion, useVersions, useRollback } from "./hooks";

function fmtDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function VersionHistoryPage() {
  const { id = "" } = useParams();
  const isAdmin = useIsAdmin();
  const { data: versions, isLoading, isError, error } = useVersions(id);
  const rollback = useRollback(id);

  const [left, setLeft] = useState<number | null>(null);
  const [right, setRight] = useState<number | null>(null);
  const [toRollback, setToRollback] = useState<number | null>(null);

  const leftQuery = useVersion(id, left);
  const rightQuery = useVersion(id, right);

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 text-slate-600">
        <Spinner /> Loading versions…
      </div>
    );
  }
  if (isError) {
    return (
      <p className="text-sm text-red-700">Failed to load versions: {(error as Error)?.message}</p>
    );
  }

  const list = versions ?? [];

  // Select a version into the first open diff slot; clicking again clears it.
  function pick(version: number): void {
    if (left === version) {
      setLeft(null);
      return;
    }
    if (right === version) {
      setRight(null);
      return;
    }
    if (left === null) setLeft(version);
    else if (right === null) setRight(version);
    else {
      // Both slots taken: replace the left (older) slot.
      setLeft(version);
    }
  }

  const canDiff = left !== null && right !== null && leftQuery.data && rightQuery.data;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Version history</h1>
        <Link to={`/profiles/${id}`} className="text-sm text-indigo-600 hover:underline">
          ← Back to editor
        </Link>
      </div>

      <p className="text-sm text-slate-500">
        Pick two versions to compare. Selected:{" "}
        {left !== null ? <Badge tone="blue">v{left}</Badge> : <span className="text-slate-400">—</span>}{" "}
        vs{" "}
        {right !== null ? (
          <Badge tone="blue">v{right}</Badge>
        ) : (
          <span className="text-slate-400">—</span>
        )}
      </p>

      <Table>
        <Thead>
          <Tr>
            <Th>Version</Th>
            <Th>Published by</Th>
            <Th>When</Th>
            <Th>Note</Th>
            <Th className="text-right">Actions</Th>
          </Tr>
        </Thead>
        <Tbody>
          {list.length === 0 ? (
            <Tr>
              <Td className="text-slate-500" colSpan={5}>
                No published versions yet.
              </Td>
            </Tr>
          ) : null}
          {list.map((v) => {
            const selected = v.version === left || v.version === right;
            return (
              <Tr key={v.version} className={selected ? "bg-blue-50" : undefined}>
                <Td className="font-medium">v{v.version}</Td>
                <Td>{v.published_by ?? <span className="text-slate-400">—</span>}</Td>
                <Td>{fmtDate(v.published_at)}</Td>
                <Td>{v.note ?? <span className="text-slate-400">—</span>}</Td>
                <Td className="text-right">
                  <div className="flex justify-end gap-2">
                    <Button variant="ghost" onClick={() => pick(v.version)}>
                      {selected ? "Deselect" : "Compare"}
                    </Button>
                    {isAdmin ? (
                      <Button variant="secondary" onClick={() => setToRollback(v.version)}>
                        Roll back
                      </Button>
                    ) : null}
                  </div>
                </Td>
              </Tr>
            );
          })}
        </Tbody>
      </Table>

      {left !== null && right !== null ? (
        <div className="space-y-2">
          <h2 className="text-sm font-semibold">
            Diff: v{left} → v{right}
          </h2>
          {leftQuery.isLoading || rightQuery.isLoading ? (
            <div className="flex items-center gap-2 text-sm text-slate-500">
              <Spinner /> Loading versions…
            </div>
          ) : canDiff && leftQuery.data && rightQuery.data ? (
            <DiffView oldConfig={leftQuery.data.config} newConfig={rightQuery.data.config} />
          ) : (
            <p className="text-sm text-red-700">Could not load one of the selected versions.</p>
          )}
        </div>
      ) : null}

      <ConfirmDialog
        open={toRollback !== null}
        title="Roll back?"
        body={
          <>
            Roll back to <strong>v{toRollback}</strong>? This publishes its config as a new version
            and makes it live. The current live version is preserved in history.
          </>
        }
        confirmLabel="Roll back"
        busy={rollback.isPending}
        onCancel={() => setToRollback(null)}
        onConfirm={() => {
          if (toRollback === null) return;
          rollback.mutate(toRollback, { onSuccess: () => setToRollback(null) });
        }}
      />
    </div>
  );
}
