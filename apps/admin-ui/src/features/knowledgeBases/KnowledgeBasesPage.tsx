import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";
import { Spinner } from "../../components/ui/spinner";
import { fmtDate } from "../../lib/format";
import { useIsAdmin } from "../../auth/useSession";
import { useKnowledgeBases } from "./hooks";
import { KnowledgeBaseFormDialog } from "./KnowledgeBaseFormDialog";

export function statusBadge(status: string) {
  if (status === "complete") return <Badge tone="green">complete</Badge>;
  if (status === "error") return <Badge tone="red">error</Badge>;
  return <Badge tone="amber">in progress</Badge>;
}

export function KnowledgeBasesPage() {
  const isAdmin = useIsAdmin();
  const navigate = useNavigate();
  const [creating, setCreating] = useState(false);
  const kbs = useKnowledgeBases();
  const list = kbs.data ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl text-ink-strong">Knowledge</h1>
        {isAdmin ? <Button onClick={() => setCreating(true)}>New knowledge base</Button> : null}
      </div>

      {kbs.isLoading ? (
        <div className="flex items-center gap-2 text-muted">
          <Spinner /> Loading knowledge bases…
        </div>
      ) : kbs.isError ? (
        <p className="text-sm text-red-700">
          Failed to load knowledge bases: {(kbs.error as Error)?.message}
        </p>
      ) : (
        <Table>
          <Thead>
            <Tr>
              <Th>Name</Th>
              <Th>Status</Th>
              <Th>Sources</Th>
              <Th>Updated</Th>
            </Tr>
          </Thead>
          <Tbody>
            {list.length === 0 ? (
              <Tr>
                <Td className="text-faint" colSpan={4}>
                  No knowledge bases yet.
                </Td>
              </Tr>
            ) : null}
            {list.map((k) => (
              <Tr key={k.id}>
                <Td className="font-medium">
                  <Link className="text-accent hover:underline" to={`/knowledge-bases/${k.id}`}>
                    {k.name}
                  </Link>
                </Td>
                <Td>{statusBadge(k.status)}</Td>
                <Td className="tabular-nums">{k.source_count}</Td>
                <Td className="whitespace-nowrap text-xs">{fmtDate(k.updated_at)}</Td>
              </Tr>
            ))}
          </Tbody>
        </Table>
      )}

      {creating ? (
        <KnowledgeBaseFormDialog
          onClose={() => setCreating(false)}
          onCreated={(id) => {
            setCreating(false);
            navigate(`/knowledge-bases/${id}`);
          }}
        />
      ) : null}
    </div>
  );
}
