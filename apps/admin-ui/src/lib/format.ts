// Shared display formatters. fmtDate is lifted verbatim from AuditPage.tsx —
// the three Calls/Queues pages end the per-page duplication going forward
// (spec §2.1); existing pages are deliberately not migrated here.

export function fmtDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function fmtDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  const m = Math.floor(seconds / 60); // minutes unbounded — 3725 renders as "62:05"
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}
