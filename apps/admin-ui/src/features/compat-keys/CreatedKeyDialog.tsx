import { Dialog } from "../../components/ui/dialog";
import { Button } from "../../components/ui/button";
import { pushToast } from "../../components/ui/toast";
import type { CompatKeyCreated } from "../../types/api";

// One-time reveal of a freshly created key's plaintext token. The server stores only a
// sha256 hash, so the token is unrecoverable after this closes — the only safe action is
// copy-and-store now; closing requires an explicit "Done", not a stray backdrop dismiss.
async function copyToken(token: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(token);
    pushToast("Token copied to clipboard", "info");
  } catch {
    pushToast("Copy failed — select and copy the token manually");
  }
}

export function CreatedKeyDialog({
  created,
  onDone,
}: {
  created: CompatKeyCreated | null;
  onDone: () => void;
}) {
  return (
    <Dialog open={created !== null} onClose={onDone} title="API key created" closeOnBackdrop={false}>
      {created !== null ? (
        <div className="space-y-3">
          <p className="text-sm text-slate-700">
            Copy this token now and store it securely. For your safety it will{" "}
            <strong>never be shown again</strong> — if you lose it, revoke this key and create
            a new one.
          </p>
          <div className="flex items-center gap-2">
            <code className="flex-1 select-all overflow-x-auto rounded-lg border border-line bg-surface-2 px-3 py-2 font-mono text-xs text-ink">
              {created.token}
            </code>
            <Button variant="secondary" onClick={() => void copyToken(created.token)}>
              Copy
            </Button>
          </div>
          <div className="mt-2 flex justify-end">
            <Button onClick={onDone}>Done</Button>
          </div>
        </div>
      ) : null}
    </Dialog>
  );
}
