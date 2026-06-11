import { useState } from "react";
import { Dialog } from "../../components/ui/dialog";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { Spinner } from "../../components/ui/spinner";
import { DiffView } from "../../components/DiffView";
import { pushToast } from "../../components/ui/toast";
import type { AgentConfig } from "../../types/api";
import { usePublish, useVersion } from "./hooks";

interface PublishDialogProps {
  open: boolean;
  onClose: () => void;
  profileId: string;
  draftConfig: AgentConfig;
  // The currently-published version number, or null if nothing is live yet.
  publishedVersion: number | null;
  onPublished: () => void;
  // Receives ApiError.detail on publish failure so the editor can route 422 field
  // errors through mapServerErrors. Exactly ONE error handler per mutation:
  // react-query v5 runs this per-mutate onError IN ADDITION to any hook-level one,
  // so usePublish deliberately has no hook-level onError (a second handler would
  // double-toast). Absent the prop, the detail is toasted here — never swallowed.
  onPublishError?: (detail: string) => void;
}

// Shows what publishing will change: the live version's config vs the current
// draft. If there is no live version yet, this is the first publish. Confirming
// calls the publish mutation with the optional note.
export function PublishDialog({
  open,
  onClose,
  profileId,
  draftConfig,
  publishedVersion,
  onPublished,
  onPublishError,
}: PublishDialogProps) {
  const [note, setNote] = useState("");
  const publish = usePublish(profileId);
  const liveQuery = useVersion(profileId, open ? publishedVersion : null);

  function handleClose(): void {
    if (publish.isPending) return;
    setNote("");
    onClose();
  }

  function handleConfirm(): void {
    publish.mutate(
      { note: note.trim() || null },
      {
        onSuccess: () => {
          setNote("");
          onPublished();
        },
        // The mutation's ONLY error handler (see onPublishError prop comment).
        onError: (err) => {
          if (onPublishError) onPublishError(err.detail);
          else pushToast(err.detail);
        },
      },
    );
  }

  const isFirstPublish = publishedVersion === null;

  return (
    <Dialog open={open} onClose={handleClose} title="Publish draft">
      <div className="space-y-4">
        {isFirstPublish ? (
          <p className="text-sm text-slate-700">
            This is the <strong>first publish</strong> for this profile. The draft below becomes
            version 1 and goes live.
          </p>
        ) : (
          <div>
            <p className="mb-2 text-sm text-slate-700">
              Changes from live version {publishedVersion} → new version:
            </p>
            {liveQuery.isLoading ? (
              <div className="flex items-center gap-2 text-sm text-slate-500">
                <Spinner /> Loading live version…
              </div>
            ) : liveQuery.isError ? (
              <p className="text-sm text-red-700">Could not load the live version to diff.</p>
            ) : liveQuery.data ? (
              <div className="max-h-72 overflow-y-auto">
                <DiffView oldConfig={liveQuery.data.config} newConfig={draftConfig} />
              </div>
            ) : null}
          </div>
        )}

        <div>
          <label className="mb-1 block text-sm font-medium text-slate-700" htmlFor="publish-note">
            Note <span className="text-slate-400">(optional)</span>
          </label>
          <Input
            id="publish-note"
            value={note}
            placeholder="What changed and why"
            onChange={(e) => setNote(e.target.value)}
          />
        </div>

        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={handleClose} disabled={publish.isPending}>
            Cancel
          </Button>
          <Button onClick={handleConfirm} disabled={publish.isPending}>
            {publish.isPending ? "Publishing…" : "Publish"}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
