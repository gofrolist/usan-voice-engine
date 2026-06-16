import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ContactSummary } from "../types/api";

const getMock = vi.fn();
const putMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    put: (u: string, b?: unknown) => putMock(u, b),
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

const pushToastMock = vi.fn();
vi.mock("../components/ui/toast", () => ({
  pushToast: (message: string, tone?: string) => pushToastMock(message, tone),
}));

import { ContactsPage } from "../features/contacts/ContactsPage";

let contacts: ContactSummary[] = [];

function routeGet(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") return Promise.resolve({ email: "me@example.com", role: "admin" });
  if (url.startsWith("/v1/admin/contacts")) return Promise.resolve(contacts);
  if (url.startsWith("/v1/admin/profiles")) return Promise.resolve([]);
  return Promise.reject(new Error(`unexpected GET ${url}`));
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ContactsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function contact(over: Partial<ContactSummary> = {}): ContactSummary {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    name: "Edna Moore",
    masked_phone: "***4567",
    timezone: "America/New_York",
    agent_profile_id: null,
    agent_profile_name: null,
    ...over,
  };
}

describe("ContactsPage timezone editor", () => {
  beforeEach(() => {
    getMock.mockReset();
    putMock.mockReset();
    pushToastMock.mockReset();
    getMock.mockImplementation(routeGet);
    contacts = [contact()];
  });
  afterEach(() => vi.clearAllMocks());

  it("renders the contact's current timezone as the selected option", async () => {
    renderPage();
    const select = await screen.findByLabelText("Timezone for Edna Moore");
    expect((select as HTMLSelectElement).value).toBe("America/New_York");
  });

  it("calls the API when the timezone is changed", async () => {
    putMock.mockResolvedValue(contact({ timezone: "America/Chicago" }));
    renderPage();
    const select = await screen.findByLabelText("Timezone for Edna Moore");
    await userEvent.selectOptions(select, "America/Chicago");
    await waitFor(() =>
      expect(putMock).toHaveBeenCalledWith(
        "/v1/admin/contacts/11111111-1111-1111-1111-111111111111/timezone",
        { timezone: "America/Chicago" },
      ),
    );
  });

  it("keeps an exotic (non-US) current zone visible and selected", async () => {
    contacts = [contact({ timezone: "Europe/London" })];
    renderPage();
    const select = await screen.findByLabelText("Timezone for Edna Moore");
    expect((select as HTMLSelectElement).value).toBe("Europe/London");
    expect(screen.getByRole("option", { name: "Europe/London" })).toBeInTheDocument();
  });
});
