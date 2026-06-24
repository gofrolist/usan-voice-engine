import { useState } from "react";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Input } from "../../components/ui/input";
import { Button } from "../../components/ui/button";
import { Spinner } from "../../components/ui/spinner";
import { Dialog } from "../../components/ui/dialog";
import { fmtDate } from "../../lib/format";
import { useIsAdmin } from "../../auth/useSession";
import { AddDncDialog } from "./AddDncDialog";
import { useDnc, useRemoveDnc } from "./hooks";

const E164 = /^\+[1-9]\d{7,14}$/;

export function DncPage() {
  const isAdmin = useIsAdmin();
  const dnc = useDnc();
  const remove = useRemoveDnc();
  const [addOpen, setAddOpen] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [removePhone, setRemovePhone] = useState("");

  if (!isAdmin) return <p className="text-sm text-muted">Admins only.</p>;

  const entries = dnc.data ?? [];
  const canRemove = E164.test(removePhone.trim());

  function closeRemove() {
    setRemoving(false);
    setRemovePhone("");
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl text-ink-strong">Do-Not-Call list</h1>
        <Button onClick={() => setAddOpen(true)}>+ Add to DNC</Button>
      </div>

      {dnc.isLoading ? (
        <div className="flex items-center gap-2 text-muted">
          <Spinner /> Loading…
        </div>
      ) : dnc.isError ? (
        <p className="text-sm text-red-700">Failed to load: {(dnc.error as Error)?.message}</p>
      ) : (
        <Table>
          <Thead>
            <Tr>
              <Th>Phone</Th>
              <Th>Reason</Th>
              <Th>Added</Th>
              <Th />
            </Tr>
          </Thead>
          <Tbody>
            {entries.length === 0 ? (
              <Tr>
                <Td className="text-faint" colSpan={4}>
                  The Do-Not-Call list is empty.
                </Td>
              </Tr>
            ) : null}
            {entries.map((e) => (
              <Tr key={e.masked_phone + e.added_at}>
                <Td className="font-mono text-xs">{e.masked_phone}</Td>
                <Td>{e.reason ?? "—"}</Td>
                <Td className="whitespace-nowrap text-xs">{fmtDate(e.added_at)}</Td>
                <Td>
                  <Button variant="danger" onClick={() => setRemoving(true)}>
                    Remove
                  </Button>
                </Td>
              </Tr>
            ))}
          </Tbody>
        </Table>
      )}

      {addOpen ? <AddDncDialog onClose={() => setAddOpen(false)} /> : null}

      <Dialog open={removing} onClose={closeRemove} title="Remove from Do-Not-Call list">
        <div className="space-y-3">
          <p className="text-sm text-slate-700">
            Entries show only a masked number, so re-enter the full E.164 to remove it. This
            re-enables calling that number.
          </p>
          <div>
            <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="dnc-remove">
              Full number (E.164)
            </label>
            <Input
              id="dnc-remove"
              placeholder="+19495554567"
              value={removePhone}
              onChange={(e) => setRemovePhone(e.target.value)}
            />
          </div>
          <div className="mt-5 flex justify-end gap-2">
            <Button
              type="button"
              variant="secondary"
              onClick={closeRemove}
              disabled={remove.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="danger"
              disabled={!canRemove || remove.isPending}
              onClick={() => remove.mutate(removePhone.trim(), { onSuccess: closeRemove })}
            >
              Remove
            </Button>
          </div>
        </div>
      </Dialog>
    </div>
  );
}
