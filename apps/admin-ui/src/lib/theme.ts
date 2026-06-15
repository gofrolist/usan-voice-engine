import { useSyncExternalStore } from "react";

// Light/dark theme store backed by the `<html class="dark">` toggle + localStorage.
// The initial class is applied pre-paint by the inline script in index.html (no flash);
// this store reads it back and keeps every <ThemeToggle> in sync via an external store
// so the desktop sidebar and the mobile top-bar toggles never drift apart.

export type Theme = "light" | "dark";

const STORAGE_KEY = "usan-theme";
const listeners = new Set<() => void>();

function read(): Theme {
  if (typeof document === "undefined") return "light";
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

export function setTheme(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // Storage unavailable (private mode) — the class is still applied for this session.
  }
  listeners.forEach((l) => l());
}

export function toggleTheme(): void {
  setTheme(read() === "dark" ? "light" : "dark");
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

export function useTheme(): Theme {
  return useSyncExternalStore(subscribe, read, () => "light");
}
