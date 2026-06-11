// apps/admin-ui/src/test/CustomVariablesPage.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// Route-by-URL api mock — /v1/auth/me is served too, so viewer/admin gating is
// driven by the real useIsAdmin query, not a mocked hook.
const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
const delMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string, b?: unknown) => postMock(u, b),
    patch: (u: string, b?: unknown) => patchMock(u, b),
    del: (u: string) => delMock(u),
  },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));

import { CustomVariablesPage } from "../features/customVariables/CustomVariablesPage";
import type { CustomVariable } from "../features/customVariables/hooks";

let role: "admin" | "viewer" = "admin";
let vars: CustomVariable[] = [];

function variable(over: Partial<CustomVariable> = {}): CustomVariable {
  return {
    id: "00000000-0000-0000-0000-000000000001",
    name: "pet_name",
    description: "The elder's pet's name.",
    example: "Rex",
    phi: false,
    created_at: "2026-06-10T09:00:00Z",
    updated_at: "2026-06-10T09:00:00Z",
    ...over,
  };
}

function routeGet(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") {
    return Promise.resolve({ email: "me@example.com", role });
  }
  if (url === "/v1/admin/custom-variables") {
    return Promise.resolve(vars);
  }
  return Promise.reject(new Error(`unexpected GET ${url}`));
}

function renderPage(): QueryClient {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <CustomVariablesPage />
    </QueryClientProvider>,
  );
  return client;
}

beforeEach(() => {
  getMock.mockReset();
  getMock.mockImplementation(routeGet);
  role = "admin";
  vars = [
    variable(),
    variable({
      id: "00000000-0000-0000-0000-000000000002",
      name: "diagnosis",
      description: "Latest diagnosis.",
      example: "stable",
      phi: true,
    }),
  ];
});
afterEach(() => vi.clearAllMocks());

describe("CustomVariablesPage", () => {
  it("renders table with name, description, example and PHI badge", async () => {
    renderPage();

    const table = await screen.findByRole("table");
    expect(within(table).getByText("pet_name")).toBeInTheDocument();
    expect(within(table).getByText("The elder's pet's name.")).toBeInTheDocument();
    expect(within(table).getByText("Rex")).toBeInTheDocument();
    expect(within(table).getByText("diagnosis")).toBeInTheDocument();
    expect(within(table).getByText("Latest diagnosis.")).toBeInTheDocument();
    // Only the phi=true row carries the badge (the column header also reads
    // "PHI", so assert per row).
    const phiRow = within(table).getByText("diagnosis").closest("tr") as HTMLElement;
    expect(within(phiRow).getByText("PHI")).toBeInTheDocument();
    const nonPhiRow = within(table).getByText("pet_name").closest("tr") as HTMLElement;
    expect(within(nonPhiRow).queryByText("PHI")).not.toBeInTheDocument();
  });

  it("create dialog posts and invalidates both query keys", async () => {
    const user = userEvent.setup();
    postMock.mockResolvedValue(variable({ name: "pharmacy_name" }));
    const client = renderPage();
    const spy = vi.spyOn(client, "invalidateQueries");

    await user.click(await screen.findByRole("button", { name: "New variable" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Name"), "pharmacy_name");
    await user.type(within(dialog).getByLabelText("Description"), "Preferred pharmacy.");
    await user.type(within(dialog).getByLabelText("Example"), "Walgreens");
    await user.click(within(dialog).getByLabelText(/PHI/));
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/admin/custom-variables", {
        name: "pharmacy_name",
        description: "Preferred pharmacy.",
        example: "Walgreens",
        phi: true,
      }),
    );
    // The variable catalog has a 5-min staleTime — without explicit invalidation
    // the editor palette/warnings would lag CRUD by minutes.
    await waitFor(() => {
      const keys = spy.mock.calls.map((c) =>
        JSON.stringify((c[0] as { queryKey?: unknown } | undefined)?.queryKey),
      );
      expect(keys).toContain(JSON.stringify(["custom-variables"]));
      expect(keys).toContain(JSON.stringify(["variable-catalog"]));
    });
  });

  it("create dialog shows the PHI help text", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New variable" }));
    expect(
      screen.getByText(
        "Names, descriptions, and examples are operator configuration — never put PHI in " +
          "them. Mark a variable PHI if its per-call value will contain health information; " +
          "PHI variables are blocked in SMS templates.",
      ),
    ).toBeInTheDocument();
  });

  it("edit dialog has no name input and PATCHes description/example/phi", async () => {
    const user = userEvent.setup();
    patchMock.mockResolvedValue(variable({ phi: true }));
    renderPage();

    const table = await screen.findByRole("table");
    const row = within(table).getByText("pet_name").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Edit" }));
    const dialog = screen.getByRole("dialog");
    // name is immutable after create (delete + recreate instead) — no input for it.
    expect(within(dialog).queryByLabelText("Name")).not.toBeInTheDocument();
    await user.click(within(dialog).getByLabelText(/PHI/));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(patchMock).toHaveBeenCalledWith(
        "/v1/admin/custom-variables/00000000-0000-0000-0000-000000000001",
        {
          description: "The elder's pet's name.",
          example: "Rex",
          phi: true,
        },
      ),
    );
  });

  it("delete confirms then DELETEs", async () => {
    const user = userEvent.setup();
    delMock.mockResolvedValue(undefined);
    renderPage();

    const table = await screen.findByRole("table");
    const row = within(table).getByText("pet_name").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog");
    // Confirm dialog first — nothing deleted until the operator confirms.
    expect(delMock).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(delMock).toHaveBeenCalledWith(
        "/v1/admin/custom-variables/00000000-0000-0000-0000-000000000001",
      ),
    );
  });

  it("mutation buttons hidden for viewer role", async () => {
    role = "viewer";
    renderPage();

    // The list itself stays readable for viewers (GET is all-session-roles).
    const table = await screen.findByRole("table");
    expect(within(table).getByText("pet_name")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New variable" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
  });
});
