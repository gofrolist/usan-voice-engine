import { Link } from "react-router-dom";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import type { SectionKey } from "../../config/fieldMeta";

function Chip({ label, value, onClick }: { label: string; value: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex items-center gap-1.5 rounded-lg border border-line bg-surface px-2.5 py-1 text-xs text-muted transition-colors hover:border-line-strong hover:bg-surface-2"
    >
      <span className="text-faint">{label}</span>
      <span className="max-w-[10rem] truncate font-medium text-ink">{value}</span>
    </button>
  );
}

interface EditorToolbarProps {
  name: string;
  status: string;
  publishedVersion: number | null;
  dirty: boolean;
  model: string;
  voice: string;
  language: string;
  isAdmin: boolean;
  saving: boolean;
  profileId: string;
  onJump: (s: SectionKey) => void;
  onSave: () => void;
  onPublish: () => void;
}

// The pinned editor header: profile identity + status, the model/voice/language at a
// glance (each jumps to its section), and the Save/Publish actions. It is a flex
// sibling of the scrolling body — not a sticky overlay — so it never covers content.
export function EditorToolbar(props: EditorToolbarProps) {
  return (
    <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-line bg-surface px-4 py-3 sm:px-6 lg:px-8">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="truncate font-display text-xl text-ink-strong">{props.name}</h1>
          <Badge tone={props.status === "active" ? "green" : "gray"}>{props.status}</Badge>
          {props.publishedVersion !== null ? (
            <Badge tone="blue">live v{props.publishedVersion}</Badge>
          ) : (
            <Badge tone="gray">unpublished</Badge>
          )}
          {props.dirty ? <Badge tone="amber">unsaved changes</Badge> : null}
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <Chip label="Model" value={props.model} onClick={() => props.onJump("llm")} />
          <Chip label="Voice" value={props.voice} onClick={() => props.onJump("voice")} />
          <Chip label="Lang" value={props.language} onClick={() => props.onJump("voice")} />
          <Link
            to={`/profiles/${props.profileId}/versions`}
            className="text-xs font-medium text-accent hover:underline"
          >
            Version history
          </Link>
        </div>
      </div>
      {props.isAdmin ? (
        <div className="flex shrink-0 gap-2">
          <Button variant="secondary" onClick={props.onSave} disabled={props.saving}>
            {props.saving ? "Saving…" : "Save draft"}
          </Button>
          <Button onClick={props.onPublish}>Publish</Button>
        </div>
      ) : (
        <span className="text-xs text-muted">Read-only (viewer role)</span>
      )}
    </div>
  );
}
