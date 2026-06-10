import { cn } from "../../lib/cn";
import { fmtDate } from "../../lib/format";
import type { TranscriptSegment } from "../../types/api";
import { isCallInProgress } from "./hooks";

interface TranscriptViewerProps {
  segments: TranscriptSegment[];
  callStatus: string;
}

// Role-styled message list, no virtualization — the server caps transcripts at
// 1000 segments (spec §5.3). Each card carries data-role as the semantic hook the
// tests assert on; the alignment/accent classes key off the same value.
export function TranscriptViewer({ segments, callStatus }: TranscriptViewerProps) {
  if (segments.length === 0) {
    return (
      <p className="text-sm text-slate-500">
        {isCallInProgress(callStatus)
          ? "Call still in progress — transcript appears after the call ends."
          : "No transcript was captured for this call."}
      </p>
    );
  }

  return (
    <div className="space-y-2">
      {segments.map((segment, i) => {
        const role = segment.tool_name ? "tool" : segment.role;
        return (
          <div
            key={i}
            data-role={role}
            className={cn(
              "max-w-xl rounded-lg border px-3 py-2 text-sm",
              role === "assistant" && "mr-auto border-slate-200 bg-white",
              role === "user" && "ml-auto border-indigo-200 bg-indigo-50",
              role === "tool" && "mr-auto border-slate-200 bg-slate-50",
            )}
          >
            {role === "tool" ? (
              <>
                <span className="rounded bg-slate-200 px-1.5 py-0.5 font-mono text-xs">
                  {segment.tool_name}
                </span>
                <details className="mt-1">
                  <summary className="cursor-pointer text-xs text-slate-500">arguments</summary>
                  <pre className="mt-1 overflow-x-auto font-mono text-xs text-slate-700">
                    {JSON.stringify(segment.tool_args, null, 2)}
                  </pre>
                </details>
              </>
            ) : (
              <p className="whitespace-pre-wrap text-slate-900">{segment.content}</p>
            )}
            <div className="mt-1 text-xs text-slate-400">{fmtDate(segment.started_at)}</div>
          </div>
        );
      })}
    </div>
  );
}
