import type { ReactNode } from "react";
import { cn } from "../../lib/cn";

type Tone = "gray" | "green" | "blue" | "red" | "amber";

const TONES: Record<Tone, string> = {
  gray: "bg-surface-2 text-muted ring-line",
  green: "bg-green-100 text-green-800 ring-green-800/15",
  blue: "bg-blue-100 text-blue-800 ring-blue-800/15",
  red: "bg-red-100 text-red-800 ring-red-800/15",
  amber: "bg-amber-100 text-amber-800 ring-amber-800/20",
};

export function Badge({ tone = "gray", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset",
        TONES[tone],
      )}
    >
      {children}
    </span>
  );
}
