import type { ButtonHTMLAttributes } from "react";
import { cn } from "../../lib/cn";

type Variant = "primary" | "accent" | "secondary" | "danger" | "ghost";

const VARIANTS: Record<Variant, string> = {
  // Inverted ink button: dark-on-light in light mode, light-on-dark in dark mode.
  primary: "bg-ink text-canvas shadow-card hover:bg-ink-strong disabled:opacity-40",
  accent: "bg-accent text-accent-fg shadow-card hover:bg-accent-strong disabled:opacity-40",
  secondary: "border border-line-strong bg-surface text-ink hover:bg-surface-2 disabled:opacity-50",
  danger: "bg-red-600 text-white shadow-card hover:bg-red-700 disabled:opacity-40",
  ghost: "bg-transparent text-muted hover:bg-surface-2 hover:text-ink disabled:opacity-50",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
}

export function Button({ variant = "primary", className, ...props }: ButtonProps) {
  return (
    <button
      className={cn(
        "inline-flex items-center justify-center gap-1.5 rounded-lg px-3.5 py-1.5 text-sm font-medium transition-[background-color,color,box-shadow,border-color] duration-150 disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-canvas",
        VARIANTS[variant],
        className,
      )}
      {...props}
    />
  );
}
