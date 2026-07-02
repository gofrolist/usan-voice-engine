import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { UserMenu } from "../components/UserMenu";
import { meFixture } from "./meFixture";

beforeEach(() => {
  // Start each test from a known theme (light) so the toggle assertions are deterministic.
  document.documentElement.classList.remove("dark");
});
afterEach(() => vi.clearAllMocks());

describe("UserMenu", () => {
  it("shows the email and role, with the menu closed by default", () => {
    render(<UserMenu me={meFixture("admin")} onLogout={vi.fn()} />);
    expect(screen.getByText("me@example.com")).toBeInTheDocument();
    expect(screen.getByText("admin")).toBeInTheDocument();
    // Menu items are not rendered until the "⋯" button is clicked.
    expect(screen.queryByRole("menuitem", { name: /log out/i })).not.toBeInTheDocument();
  });

  it("opens the popover and calls onLogout when Log out is clicked", async () => {
    const onLogout = vi.fn();
    render(<UserMenu me={meFixture("admin")} onLogout={onLogout} />);
    await userEvent.click(screen.getByRole("button", { name: "Account menu" }));
    await userEvent.click(screen.getByRole("menuitem", { name: /log out/i }));
    expect(onLogout).toHaveBeenCalledTimes(1);
    // The menu closes after selecting Log out.
    expect(screen.queryByRole("menuitem", { name: /log out/i })).not.toBeInTheDocument();
  });

  it("toggles the theme from the Appearance item (light -> dark)", async () => {
    render(<UserMenu me={meFixture("admin")} onLogout={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Account menu" }));
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    await userEvent.click(screen.getByRole("menuitem", { name: /appearance/i }));
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("closes the menu on Escape", async () => {
    render(<UserMenu me={meFixture("admin")} onLogout={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Account menu" }));
    expect(screen.getByRole("menuitem", { name: /log out/i })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("menuitem", { name: /log out/i })).not.toBeInTheDocument();
  });
});
