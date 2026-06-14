// apps/admin-ui/src/test/ToolsSection.test.tsx
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useForm, type Resolver, type UseFormReturn } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import type { ReactNode } from "react";
import { ToolsSection } from "../features/editor/sections/ToolsSection";
import {
  agentConfigSchema,
  smsTemplateSchema,
  type AgentConfigForm,
} from "../config/agentConfigSchema";
import type { ToolSpec } from "../config/toolCatalog";
import type { VariableSpec } from "../config/variableCatalog";

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
    resolver: zodResolver(agentConfigSchema) as Resolver<AgentConfigForm>,
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

// Catalog slice for the catalog-driven SMS notices (spec §6.1): one PHI custom, one
// non-PHI custom, one builtin. Served through the route-by-URL api mock — the real
// useVariableCatalog hook fetches /v1/admin/variable-catalog.
const VAR_CATALOG: VariableSpec[] = [
  {
    name: "first_name",
    tier: "builtin",
    description: "Elder first name",
    default: "there",
    example: "Rose",
    phi: false,
  },
  {
    name: "diagnosis",
    tier: "custom",
    description: "Primary diagnosis",
    default: "",
    example: "diabetes",
    phi: true,
  },
  {
    name: "pet_name",
    tier: "custom",
    description: "Pet name",
    default: "",
    example: "Biscuit",
    phi: false,
  },
];

function mockCatalogs(): void {
  getMock.mockImplementation((url: string) =>
    url === "/v1/admin/variable-catalog"
      ? Promise.resolve({ variables: VAR_CATALOG })
      : Promise.resolve({ tools: CATALOG }),
  );
}

// Harness with one pre-populated SMS template body so the catalog-driven notices
// (computed from the watched body value) can be asserted directly.
function SmsBodyHarness({ body }: { body: string }) {
  const form = useForm<AgentConfigForm>({
    resolver: zodResolver(agentConfigSchema) as Resolver<AgentConfigForm>,
    defaultValues: {
      tools: {
        enabled: ["send_sms", "end_call"],
        sms: { templates: [{ key: "followup", label: "Follow up", body }] },
      },
    } as unknown as AgentConfigForm,
  });
  return <ToolsSection form={form} />;
}

describe("ToolsSection SMS catalog notices", () => {
  it("sms body with phi custom shows blocked-at-save notice", async () => {
    mockCatalogs();
    render(wrapper(<SmsBodyHarness body="Hi {{diagnosis}}" />));

    const notice = await screen.findByText(/blocked at save/i);
    expect(notice.textContent).toContain("{{diagnosis}}");
    expect(notice.textContent).toMatch(/PHI/);
    // Non-blocking client-side: the static zod schema stays frozen on the 5 builtin
    // PHI names, so a PHI *custom* still passes zod — the server 422 is authoritative.
    expect(
      smsTemplateSchema.safeParse({
        key: "followup",
        label: "Follow up",
        body: "Hi {{diagnosis}}",
      }).success,
    ).toBe(true);
  });

  it("sms body with any custom shows renders-empty notice", async () => {
    mockCatalogs();
    render(wrapper(<SmsBodyHarness body="Hi {{pet_name}}" />));

    const notice = await screen.findByText(/not substituted in SMS/i);
    expect(notice.textContent).toContain("{{pet_name}}");
    expect(notice.textContent).toMatch(/renders empty/i);
    // pet_name is not PHI, so the blocked-at-save notice must NOT render.
    expect(screen.queryByText(/blocked at save/i)).not.toBeInTheDocument();
  });

  it("builtin non-phi token shows neither notice", async () => {
    mockCatalogs();
    render(wrapper(<SmsBodyHarness body="Hi {{first_name}}" />));

    // Wait for the catalogs to settle so the absence assertions are meaningful.
    await screen.findByText("Record the elder's wellness.");
    await waitFor(() => expect(getMock).toHaveBeenCalledWith("/v1/admin/variable-catalog"));
    expect(screen.queryByText(/blocked at save/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/not substituted in SMS/i)).not.toBeInTheDocument();
  });
});
