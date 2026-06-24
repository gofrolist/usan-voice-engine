import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { ScheduleResponse } from "../types/api";

const postMock = vi.fn();
const patchMock = vi.fn();
const getMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string, b?: unknown) => postMock(u, b),
    patch: (u: string, b?: unknown) => patchMock(u, b),
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

import { ScheduleFormDialog } from "../features/schedules/ScheduleFormDialog";

const sched: ScheduleResponse = {
  id: "s1",
  contact_id: "c1",
  slot: "morning",
  enabled: true,
  window_start_local: "09:00:00",
  window_end_local: "11:00:00",
  days_of_week: ["monday", "tuesday"],
  dynamic_vars: {},
  profile_override: null,
  next_run_at: "2026-06-24T13:00:00Z",
  last_materialized_date: null,
  last_result: null,
  last_result_at: null,
  created_at: "",
  updated_at: "",
};

function renderDialog(node: ReactNode) {
  getMock.mockImplementation((u: string) =>
    u.startsWith("/v1/admin/profiles") ? Promise.resolve([]) : Promise.reject(new Error(u)),
  );
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

beforeEach(() => {
  postMock.mockReset();
  patchMock.mockReset();
  getMock.mockReset();
});
afterEach(() => vi.clearAllMocks());

describe("ScheduleFormDialog — create", () => {
  it("blocks an inverted window", async () => {
    const user = userEvent.setup();
    renderDialog(
      <ScheduleFormDialog mode="create" contactId="c1" existingSlots={[]} onClose={vi.fn()} />,
    );
    await user.clear(screen.getByLabelText("Window start"));
    await user.type(screen.getByLabelText("Window start"), "11:00");
    await user.clear(screen.getByLabelText("Window end"));
    await user.type(screen.getByLabelText("Window end"), "09:00");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(postMock).not.toHaveBeenCalled();
    expect(screen.getByText(/start.*before.*end/i)).toBeInTheDocument();
  });

  it("requires at least one day", async () => {
    const user = userEvent.setup();
    renderDialog(
      <ScheduleFormDialog mode="create" contactId="c1" existingSlots={[]} onClose={vi.fn()} />,
    );
    await user.type(screen.getByLabelText("Window start"), "09:00");
    await user.type(screen.getByLabelText("Window end"), "11:00");
    for (const d of [
      "monday",
      "tuesday",
      "wednesday",
      "thursday",
      "friday",
      "saturday",
      "sunday",
    ]) {
      const cb = screen.getByLabelText(d) as HTMLInputElement;
      if (cb.checked) await user.click(cb);
    }
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(postMock).not.toHaveBeenCalled();
    expect(screen.getByText(/at least one day/i)).toBeInTheDocument();
  });

  it("posts a valid morning schedule", async () => {
    const user = userEvent.setup();
    postMock.mockResolvedValue(sched);
    const onClose = vi.fn();
    renderDialog(
      <ScheduleFormDialog mode="create" contactId="c1" existingSlots={[]} onClose={onClose} />,
    );
    await user.type(screen.getByLabelText("Window start"), "09:00");
    await user.type(screen.getByLabelText("Window end"), "11:00");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(postMock).toHaveBeenCalledTimes(1));
    const [, body] = postMock.mock.calls[0];
    expect(body).toMatchObject({
      contact_id: "c1",
      slot: "morning",
      window_start_local: "09:00",
      window_end_local: "11:00",
    });
    expect(body.days_of_week.length).toBe(7);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("only offers the free slot when one slot is taken", async () => {
    renderDialog(
      <ScheduleFormDialog
        mode="create"
        contactId="c1"
        existingSlots={["morning"]}
        onClose={vi.fn()}
      />,
    );
    const slot = screen.getByLabelText("Slot") as HTMLSelectElement;
    const values = Array.from(slot.options).map((o) => o.value);
    expect(values).toEqual(["evening"]);
  });
});

describe("ScheduleFormDialog — edit", () => {
  it("renders the slot read-only and PATCHes window+days together", async () => {
    const user = userEvent.setup();
    patchMock.mockResolvedValue(sched);
    renderDialog(
      <ScheduleFormDialog
        mode="edit"
        contactId="c1"
        schedule={sched}
        existingSlots={["morning"]}
        onClose={vi.fn()}
      />,
    );
    expect(screen.queryByLabelText("Slot")).not.toBeInTheDocument();
    expect(screen.getByText(/morning/i)).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Window end"));
    await user.type(screen.getByLabelText("Window end"), "12:00");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));
    const [, body] = patchMock.mock.calls[0];
    expect(body.window_start_local).toBe("09:00");
    expect(body.window_end_local).toBe("12:00");
    expect(body.slot).toBeUndefined();
    expect("dynamic_vars" in body).toBe(false);
  });
});
