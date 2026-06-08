import { useSyncExternalStore } from "react";

export interface Toast {
  id: number;
  message: string;
  tone: "error" | "info";
}

let toasts: Toast[] = [];
let nextId = 1;
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getSnapshot(): Toast[] {
  return toasts;
}

export function pushToast(message: string, tone: Toast["tone"] = "error"): number {
  const id = nextId++;
  toasts = [...toasts, { id, message, tone }];
  emit();
  return id;
}

export function dismissToast(id: number): void {
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}

// Hook used by <ErrorToast>. Pure read of the external store.
export function useToasts(): Toast[] {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
