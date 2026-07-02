import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Input } from "../../components/ui/input";
import { Textarea } from "../../components/ui/textarea";
import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";
import { Spinner } from "../../components/ui/spinner";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { fmtDate } from "../../lib/format";
import { useIsAdmin } from "../../auth/useSession";
import type { ApiError } from "../../lib/api";
import { statusBadge } from "./KnowledgeBasesPage";
import { useAddSource, useDeleteKb, useDeleteSource, useKnowledgeBase } from "./hooks";

function sourceBadge(status: string) {
  return status === "embedded" ? (
    <Badge tone="green">embedded</Badge>
  ) : (
    <Badge tone="amber">pending</Badge>
  );
}

export function KnowledgeBaseDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const isAdmin = useIsAdmin();
  const kb = useKnowledgeBase(id);
  const addSource = useAddSource(id);
  const deleteSource = useDeleteSource(id);
  const deleteKb = useDeleteKb();

  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const [confirmDeleteKb, setConfirmDeleteKb] = useState(false);
  const serverError = (addSource.error as ApiError | null)?.detail;

  function handleAdd(e: FormEvent): void {
    e.preventDefault();
    setLocalError(null);
    if (title.trim().length === 0) {
      setLocalError("Title is required.");
      return;
    }
    if (text.trim().length === 0) {
      setLocalError("Text is required.");
      return;
    }
    addSource.mutate(
      { title: title.trim(), text: text.trim() },
      {
        onSuccess: () => {
          setTitle("");
          setText("");
        },
      },
    );
  }

  if (kb.isLoading) {
    return (
      <div className="flex items-center gap-2 text-muted">
        <Spinner /> Loading…
      </div>
    );
  }
  if (kb.isError || !kb.data) {
    return (
      <div className="space-y-3">
        <p className="text-sm text-red-700">Knowledge base not found.</p>
        <Link className="text-accent hover:underline" to="/knowledge-bases">
          Back to knowledge bases
        </Link>
      </div>
    );
  }

  const data = kb.data;

  return (
    <div className="space-y-5">
      <div>
        <Link className="text-xs text-muted hover:underline" to="/knowledge-bases">
          ← Knowledge
        </Link>
        <div className="mt-1 flex flex-wrap items-center justify-between gap-3">
          <h1 className="font-display text-2xl text-ink-strong">{data.name}</h1>
          <div className="flex items-center gap-3">
            {statusBadge(data.status)}
            {isAdmin ? (
              <Button variant="secondary" onClick={() => setConfirmDeleteKb(true)}>
                Delete
              </Button>
            ) : null}
          </div>
        </div>
        {data.status === "error" && data.error_detail ? (
          <p className="mt-2 text-sm text-red-700">Ingestion error: {data.error_detail}</p>
        ) : null}
      </div>

      <section className="space-y-2">
        <h2 className="text-sm font-semibold text-ink-strong">Sources</h2>
        <Table>
          <Thead>
            <Tr>
              <Th>Title</Th>
              <Th>Status</Th>
              <Th>Added</Th>
              {isAdmin ? <Th> </Th> : null}
            </Tr>
          </Thead>
          <Tbody>
            {data.sources.length === 0 ? (
              <Tr>
                <Td className="text-faint" colSpan={isAdmin ? 4 : 3}>
                  No sources yet.
                </Td>
              </Tr>
            ) : null}
            {data.sources.map((s) => (
              <Tr key={s.id}>
                <Td className="font-medium">{s.title ?? "—"}</Td>
                <Td>{sourceBadge(s.status)}</Td>
                <Td className="whitespace-nowrap text-xs">{fmtDate(s.created_at)}</Td>
                {isAdmin ? (
                  <Td>
                    <Button
                      variant="secondary"
                      onClick={() => deleteSource.mutate(s.id)}
                      disabled={deleteSource.isPending}
                    >
                      Remove
                    </Button>
                  </Td>
                ) : null}
              </Tr>
            ))}
          </Tbody>
        </Table>
      </section>

      {isAdmin ? (
        <section className="max-w-xl space-y-2">
          <h2 className="text-sm font-semibold text-ink-strong">Add text source</h2>
          <form onSubmit={handleAdd} className="space-y-3">
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="kb-src-title">
                Title
              </label>
              <Input id="kb-src-title" value={title} onChange={(e) => setTitle(e.target.value)} />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="kb-src-text">
                Text
              </label>
              <Textarea
                id="kb-src-text"
                rows={6}
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
            </div>
            {localError ? <p className="text-xs font-medium text-red-700">{localError}</p> : null}
            {serverError ? <p className="text-xs font-medium text-red-700">{serverError}</p> : null}
            <div className="flex justify-end">
              <Button type="submit" disabled={addSource.isPending}>
                {addSource.isPending ? "Adding…" : "Add source"}
              </Button>
            </div>
          </form>
        </section>
      ) : null}

      <ConfirmDialog
        open={confirmDeleteKb}
        title="Delete knowledge base?"
        body={
          <>
            This permanently deletes <strong>{data.name}</strong> and all its sources.
          </>
        }
        confirmLabel="Delete"
        busy={deleteKb.isPending}
        onCancel={() => setConfirmDeleteKb(false)}
        onConfirm={() =>
          deleteKb.mutate(data.id, {
            onSuccess: () => navigate("/knowledge-bases"),
          })
        }
      />
    </div>
  );
}
