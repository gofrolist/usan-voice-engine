// apps/admin-ui/src/test/theme.test.tsx
import { afterEach, describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setTheme, toggleTheme } from "../lib/theme";
import { ThemeToggle } from "../components/ui/ThemeToggle";

afterEach(() => {
  document.documentElement.classList.remove("dark");
  localStorage.clear();
});

describe("theme store", () => {
  it("setTheme('dark') adds the .dark class on <html> and persists to localStorage", () => {
    setTheme("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(localStorage.getItem("usan-theme")).toBe("dark");
  });

  it("setTheme('light') removes the .dark class and persists", () => {
    setTheme("dark");
    setTheme("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(localStorage.getItem("usan-theme")).toBe("light");
  });

  it("toggleTheme flips between light and dark", () => {
    setTheme("light");
    toggleTheme();
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    toggleTheme();
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });
});

describe("ThemeToggle", () => {
  it("toggles the theme and flips its accessible label", async () => {
    setTheme("light");
    const user = userEvent.setup();
    render(<ThemeToggle />);

    await user.click(screen.getByRole("button", { name: /switch to dark theme/i }));
    expect(document.documentElement.classList.contains("dark")).toBe(true);

    // The store is shared (useSyncExternalStore), so the toggle re-renders to the
    // opposite action label after the class flips.
    await user.click(screen.getByRole("button", { name: /switch to light theme/i }));
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });
});
