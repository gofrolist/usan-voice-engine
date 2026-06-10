// apps/admin-ui/src/test/ToolsSection.test.tsx
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useForm, type UseFormReturn } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import type { ReactNode } from "react";
import { ToolsSection } from "../features/editor/sections/ToolsSection";
import { agentConfigSchema, type AgentConfigForm } from "../config/agentConfigSchema";
import type { ToolSpec } from "../config/toolCatalog";

const getMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (u: string) => getMock(u) },
}));

// A small slice of the catalog, intentionally NOT in canonical TOOL_NAMES order, so
// the order assertion proves the section sorts by TOOL_NAMES (not by catalog order).
const CATALOG: ToolSpec[] = [
  {
    name: "send_sms",
    label: "Send SMS",
    description: "Send a templated text after the call.",
    category: "messaging",
    always_on: false,
    requires_config: true,
  },
  {
    name: "log_wellness",
    label: "Log wellness",
    description: "Record the elder's wellness.",
    category: "logging",
    always_on: false,
    requires_config: false,
  },
  {
    name: "end_call",
    label: "End call",
    description: "End the call gracefully.",
    category: "lifecycle",
    always_on: true,
    requires_config: false,
  },
];

// Harness exposes the live form so tests can read tools.enabled after interactions.
let formRef: UseFormReturn<AgentConfigForm> | null = null;

function Harness({ enabled }: { enabled: string[] }) {
  const form = useForm<AgentConfigForm>({
    defaultValues: { tools: { enabled } } as AgentConfigForm,
  });
  formRef = form;
  return <ToolsSection form={form} />;
}

// The SMS hint derives from form.watch state (synchronously available) and does not
// depend on the tool catalog fetch, so these tests assert synchronously with no
// mocked catalog response (the query is left pending). The QueryClientProvider is
// still needed only because ToolsSection unconditionally calls useToolCatalog. The
// resolver + sms:null default wire the Zod schema in so the PHI-body validation path
// is exercised through the real agentConfigSchema, per Task D14 Step 1.
function SmsHarness({ enabled }: { enabled: string[] }) {
  const form = useForm<AgentConfigForm>({
    resolver: zodResolver(agentConfigSchema),
    defaultValues: {
      tools: { enabled, sms: null },
    } as unknown as AgentConfigForm,
  });
  return <ToolsSection form={form} />;
}

function wrapper(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function enabledValue(): string[] {
  return formRef!.getValues("tools.enabled");
}

afterEach(() => {
  vi.restoreAllMocks();
  getMock.mockReset();
  formRef = null;
});

describe("ToolsSection", () => {
  it("renders one row + description per catalog tool in canonical TOOL_NAMES order", async () => {
    getMock.mockResolvedValue({ tools: CATALOG });
    render(wrapper(<Harness enabled={["log_wellness", "end_call"]} />));

    await screen.findByText("Record the elder's wellness.");
    const labels = screen.getAllByText(/^(log_wellness|send_sms|end_call)$/);
    // TOOL_NAMES order: log_wellness comes before send_sms comes before end_call,
    // even though the catalog returned them as send_sms, log_wellness, end_call.
    expect(labels.map((el) => el.textContent)).toEqual(["log_wellness", "send_sms", "end_call"]);
  });

  it("renders end_call locked-on (checked + disabled)", async () => {
    getMock.mockResolvedValue({ tools: CATALOG });
    render(wrapper(<Harness enabled={["end_call"]} />));

    const endCall = (await screen.findByLabelText(/end_call/i)) as HTMLInputElement;
    expect(endCall.checked).toBe(true);
    expect(endCall.disabled).toBe(true);
  });

  it("force-adds always_on tools to the form value when the stored draft omits them", async () => {
    // The bug guard: a stored draft missing end_call must NOT silently submit without
    // it. Once the catalog loads, end_call (always_on) is unioned into enabled[].
    getMock.mockResolvedValue({ tools: CATALOG });
    render(wrapper(<Harness enabled={["log_wellness"]} />));

    await waitFor(() => expect(enabledValue()).toContain("end_call"));
    // canonical order preserved: log_wellness before end_call.
    expect(enabledValue()).toEqual(["log_wellness", "end_call"]);
  });

  it("toggling a tool preserves canonical TOOL_NAMES order in the form value", async () => {
    const user = userEvent.setup();
    getMock.mockResolvedValue({ tools: CATALOG });
    render(wrapper(<Harness enabled={["end_call"]} />));

    // Enable send_sms last; the value must still be ordered log? send_sms end_call.
    const sendSms = (await screen.findByLabelText(/send_sms/i)) as HTMLInputElement;
    await user.click(sendSms);
    await waitFor(() => expect(enabledValue()).toContain("send_sms"));
    // send_sms precedes end_call in TOOL_NAMES, so order is [send_sms, end_call].
    expect(enabledValue()).toEqual(["send_sms", "end_call"]);
  });

  it("does not let an always_on tool be toggled off (disabled input is inert)", async () => {
    const user = userEvent.setup();
    getMock.mockResolvedValue({ tools: CATALOG });
    render(wrapper(<Harness enabled={["end_call"]} />));

    const endCall = (await screen.findByLabelText(/end_call/i)) as HTMLInputElement;
    await user.click(endCall);
    // end_call must remain enabled (the toggle guard + disabled attr keep it on).
    expect(enabledValue()).toContain("end_call");
  });

  it("renders no tool rows when the catalog is empty", async () => {
    getMock.mockResolvedValue({ tools: [] });
    render(wrapper(<Harness enabled={["log_wellness"]} />));

    await waitFor(() => expect(getMock).toHaveBeenCalled());
    expect(screen.queryByText("Record the elder's wellness.")).not.toBeInTheDocument();
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
  });

  it("shows an error message and no rows when the catalog fetch fails", async () => {
    getMock.mockRejectedValue(new Error("boom"));
    render(wrapper(<Harness enabled={["log_wellness"]} />));

    expect(
      await screen.findByText("Could not load tool catalog — please refresh."),
    ).toBeInTheDocument();
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
  });
});

describe("ToolsSection SMS", () => {
  it("shows a needs-templates hint when send_sms is enabled but no templates exist", () => {
    render(wrapper(<SmsHarness enabled={["send_sms", "end_call"]} />));

    expect(screen.getByText(/needs templates/i)).toBeInTheDocument();
  });

  it("does not show the hint when send_sms is not enabled", () => {
    render(wrapper(<SmsHarness enabled={["log_wellness", "end_call"]} />));

    expect(screen.queryByText(/needs templates/i)).not.toBeInTheDocument();
  });
});
