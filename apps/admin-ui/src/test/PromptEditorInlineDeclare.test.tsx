// apps/admin-ui/src/test/PromptEditorInlineDeclare.test.tsx
// Inline variable declaration (US1): declare an undeclared {{token}} without leaving
// the prompt editor. Covers the per-token chip + "Declare all remaining", the
// builtin-collision mirror, the PHI badge in the palette, and the self-clearing
// warning — all on the Monaco textarea-fallback path (jsdom never mounts Monaco).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { FormEvent, ReactElement } from "react";

const postMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: vi.fn(),
    post: (u: string, b?: unknown) => postMock(u, b),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
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

vi.mock("../components/ui/toast", () => ({ pushToast: vi.fn() }));

import { PromptEditor } from "../features/editor/sections/PromptEditor";
import { DeclareVariableDialog } from "../features/customVariables/DeclareVariableDialog";
import type { VariableSpec } from "../config/variableCatalog";

const VARS: VariableSpec[] = [
  {
    name: "first_name",
    tier: "builtin",
    description: "The elder's first name.",
    default: "there",
    example: "Margaret",
    phi: false,
  },
  {
    name: "today_meds",
    tier: "builtin",
    description: "Medications scheduled today.",
    default: "",
    example: "Lisinopril",
    phi: true,
  },
];

function renderWithClient(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

beforeEach(() => postMock.mockReset());
afterEach(() => vi.clearAllMocks());

describe("PromptEditor inline declare", () => {
  it("renders a Declare chip per undeclared token on the textarea-fallback path", () => {
    renderWithClient(
      <PromptEditor
        id="prompts.greeting"
        value="Hi {{first_name}}, {{promo}} and {{med_name}}"
        onChange={() => {}}
        variables={VARS}
        knownNames={new Set(["first_name"])}
      />,
    );
    // One chip per unknown token (built-in first_name is NOT offered).
    expect(screen.getByRole("button", { name: /Declare\s*\{\{promo\}\}/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Declare\s*\{\{med_name\}\}/ })).toBeInTheDocument();
    // "Declare all remaining" appears when there is more than one unknown.
    expect(screen.getByRole("button", { name: /Declare all remaining/i })).toBeInTheDocument();
  });

  it("declaring a token inline POSTs the prefilled name", async () => {
    const user = userEvent.setup();
    postMock.mockResolvedValue({
      id: "1",
      name: "promo",
      description: "",
      example: "",
      phi: false,
      created_at: "2026-06-13T00:00:00Z",
      updated_at: "2026-06-13T00:00:00Z",
    });
    renderWithClient(
      <PromptEditor
        id="prompts.greeting"
        value="Hi {{promo}}"
        onChange={() => {}}
        variables={VARS}
        knownNames={new Set(["first_name"])}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Declare\s*\{\{promo\}\}/ }));
    const dialog = screen.getByRole("dialog");
    // The name is prefilled (read-only) so the declared token matches exactly.
    expect(within(dialog).getByLabelText("Name")).toHaveValue("promo");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith(
        "/v1/admin/custom-variables",
        expect.objectContaining({ name: "promo" }),
      ),
    );
  });

  it("inline Create does not submit the surrounding page form", async () => {
    // ProfileEditorPage wraps the editor in <form onSubmit={onSave}>. The inline
    // declare dialog renders its own <form>, so its Create submit must stay
    // contained and never bubble to the page form — otherwise (nested <form>s) the
    // browser submitted the page form and reloaded the editor without creating the
    // variable. The Dialog now portals out + stops submit propagation.
    const user = userEvent.setup();
    postMock.mockResolvedValue({
      id: "1",
      name: "promo",
      description: "",
      example: "",
      phi: false,
      created_at: "2026-06-13T00:00:00Z",
      updated_at: "2026-06-13T00:00:00Z",
    });
    const outerSubmit = vi.fn((e: FormEvent) => e.preventDefault());
    renderWithClient(
      <form onSubmit={outerSubmit}>
        <PromptEditor
          id="prompts.greeting"
          value="Hi {{promo}}"
          onChange={() => {}}
          variables={VARS}
          knownNames={new Set(["first_name"])}
        />
      </form>,
    );
    await user.click(screen.getByRole("button", { name: /Declare\s*\{\{promo\}\}/ }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith(
        "/v1/admin/custom-variables",
        expect.objectContaining({ name: "promo" }),
      ),
    );
    expect(outerSubmit).not.toHaveBeenCalled();
  });

  it("'Declare all remaining' POSTs every undeclared token", async () => {
    const user = userEvent.setup();
    postMock.mockResolvedValue({});
    renderWithClient(
      <PromptEditor
        id="prompts.greeting"
        value="{{promo}} and {{med_name}}"
        onChange={() => {}}
        variables={VARS}
        knownNames={new Set(["first_name"])}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Declare all remaining/i }));

    await waitFor(() => expect(postMock).toHaveBeenCalledTimes(2));
    const names = postMock.mock.calls.map((c) => (c[1] as { name: string }).name);
    expect(names).toEqual(expect.arrayContaining(["promo", "med_name"]));
  });

  it("clears the unknown-variable warning once the token becomes known", () => {
    const { rerender } = renderWithClient(
      <PromptEditor
        id="prompts.greeting"
        value="Hi {{promo}}"
        onChange={() => {}}
        variables={VARS}
        knownNames={new Set(["first_name"])}
      />,
    );
    expect(screen.getByText(/unknown variable: promo/i)).toBeInTheDocument();
    // Simulate the catalog refetch after declaring: the parent now passes promo as
    // known — the warning + chip self-clear (FR-003).
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    rerender(
      <QueryClientProvider client={client}>
        <PromptEditor
          id="prompts.greeting"
          value="Hi {{promo}}"
          onChange={() => {}}
          variables={VARS}
          knownNames={new Set(["first_name", "promo"])}
        />
      </QueryClientProvider>,
    );
    expect(screen.queryByText(/unknown variable: promo/i)).not.toBeInTheDocument();
  });
});

describe("DeclareVariableDialog collision mirror", () => {
  it("blocks a name that collides with a builtin", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn();
    renderWithClient(
      <DeclareVariableDialog
        busy={false}
        onCancel={() => {}}
        onCreate={onCreate}
        builtinNames={new Set(["first_name"])}
      />,
    );
    await user.type(screen.getByLabelText("Name"), "first_name");
    expect(screen.getByText(/collides with a built-in variable/i)).toBeInTheDocument();
    // Create is disabled and submitting does nothing (server stays authoritative).
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
  });
});

describe("VariablePalette PHI badge", () => {
  it("marks PHI variables with a badge in the palette", async () => {
    const user = userEvent.setup();
    renderWithClient(
      <PromptEditor
        id="prompts.greeting"
        value="Hello"
        onChange={() => {}}
        variables={VARS}
        knownNames={new Set(["first_name", "today_meds"])}
      />,
    );
    await user.click(screen.getByRole("button", { name: /insert variable/i }));
    const phiOption = screen.getByRole("button", { name: /today_meds/ });
    expect(within(phiOption).getByText("PHI")).toBeInTheDocument();
  });
});
