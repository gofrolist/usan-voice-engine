import { isCallInProgress } from "./hooks";

interface RecordingPlayerProps {
  url: string | null;
  ttlS: number | null;
  hasRecording: boolean;
  callStatus: string;
}

// Status-aware recording player (spec §5.3). The presigned URL is a bearer secret:
// it lives only in this prop and the <audio> src — never in query keys,
// localStorage, console.*, or rendered text.
export function RecordingPlayer({ url, ttlS, hasRecording, callStatus }: RecordingPlayerProps) {
  if (url !== null) {
    return (
      <div className="space-y-1">
        <audio controls preload="none" src={url} className="w-full" />
        {ttlS !== null ? (
          <p className="text-xs text-slate-500">
            Recording link expires in ~{Math.round(ttlS / 60)} min — reload the page for a fresh
            link.
          </p>
        ) : null}
      </div>
    );
  }
  if (hasRecording) {
    // Deliberately generic: the server returns null for a signing failure AND for an
    // unconfigured bucket — "try reloading" would be a lie in the second case.
    return (
      <p className="text-sm text-slate-500">
        Recording exists but no playback link is available right now.
      </p>
    );
  }
  if (isCallInProgress(callStatus)) {
    return (
      <p className="text-sm text-slate-500">
        Call still in progress — recording appears after the call ends.
      </p>
    );
  }
  return <p className="text-sm text-slate-500">No recording for this call.</p>;
}
