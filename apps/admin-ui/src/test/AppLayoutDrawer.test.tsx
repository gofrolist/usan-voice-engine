// apps/admin-ui/src/test/AppLayoutDrawer.test.tsx
//
// Covers the mobile-drawer accessibility added in the redesign: open from the menu
// button, Escape-to-close with focus restored to the trigger, and body scroll-lock.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

const getMock = vi.fn();
const postMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (u: string) => getMock(u), post: (u: string) => postMock(u) },
}));

import { AppLayout } from "../components/AppLayout";
import { meFixture } from "./meFixture";

function renderApp(): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="/" element={<div>home content</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  getMock.mockImplementation((url: string) =>
    url === "/v1/auth/me"
      ? Promise.resolve(meFixture("admin"))
      : Promise.reject(new Error(`unexpected GET ${url}`)),
  );
  document.documentElement.classList.remove("dark");
});

afterEach(() => {
  document.body.style.overflow = "";
});

describe("AppLayout mobile drawer", () => {
  it("opens from the menu button and exposes a labelled dialog", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(screen.getByRole("button", { name: /open navigation menu/i }));
    expect(await screen.findByRole("dialog", { name: "Navigation" })).toBeInTheDocument();
  });

  it("closes on Escape and restores focus to the menu button", async () => {
    const user = userEvent.setup();
    renderApp();
    const menuBtn = screen.getByRole("button", { name: /open navigation menu/i });
    await user.click(menuBtn);
    await screen.findByRole("dialog", { name: "Navigation" });

    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Navigation" })).toBeNull(),
    );
    expect(menuBtn).toHaveFocus();
  });

  it("locks body scroll while open and restores it on close", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(screen.getByRole("button", { name: /open navigation menu/i }));
    await screen.findByRole("dialog", { name: "Navigation" });
    expect(document.body.style.overflow).toBe("hidden");

    await user.keyboard("{Escape}");
    await waitFor(() => expect(document.body.style.overflow).not.toBe("hidden"));
  });
});
