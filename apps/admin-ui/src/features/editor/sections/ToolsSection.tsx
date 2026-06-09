import { useEffect } from "react";
import { Controller, useFieldArray, type UseFormReturn } from "react-hook-form";
import type { AgentConfigForm, ToolName } from "../../../config/agentConfigSchema";
import { TOOL_NAMES } from "../../../config/agentConfigSchema";
import { useToolCatalog, type ToolSpec } from "../../../config/toolCatalog";

// Retell-style "Functions" list: each enabled tool is a function the agent can call
// mid-call. The catalog (useToolCatalog) is the runtime source of truth for what
// renders; TOOL_NAMES is the canonical order used to rebuild the enabled[] array so
// diffs stay stable. The catalog degrades gracefully: while loading no rows render,
// and on error we surface a message so the user never mistakes a failed fetch for an
// intentionally empty tool set (which would silently omit always_on tools like
// end_call). always_on tools are force-added to the form value once the catalog
// loads (the disabled checkbox is UI-only — see the effect below).
export function ToolsSection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const error = form.formState.errors.tools?.enabled?.message;
  const { data: catalog, isError, isLoading } = useToolCatalog();
  // Pair each known ToolName with its catalog spec, in canonical TOOL_NAMES order and
  // restricted to names the catalog actually returned. Carrying the ToolName keeps the
  // toggle type-safe (enabled[] is ToolName[]); the spec drives the rendered labels.
  const byName = new Map<string, ToolSpec>(catalog?.map((t) => [t.name, t]) ?? []);
  const tools = TOOL_NAMES.flatMap((name) => {
    const spec = byName.get(name);
    return spec ? [{ name, spec }] : [];
  });

  // Guarantee every always_on tool is actually present in the form value. The disabled
  // checkbox alone is UI-only: a stored draft that omits e.g. end_call would render the
  // box visually checked (lockedOn) but submit a config without it. When the catalog
  // loads we union any missing always_on names into enabled[], in canonical order.
  const setValue = form.setValue;
  const getValues = form.getValues;
  useEffect(() => {
    if (!catalog) return;
    const alwaysOn = catalog.filter((s) => s.always_on).map((s) => s.name);
    if (alwaysOn.length === 0) return;
    const current = new Set(getValues("tools.enabled"));
    const missing = alwaysOn.filter((name) => !current.has(name as ToolName));
    if (missing.length === 0) return;
    const next = new Set([...current, ...(missing as ToolName[])]);
    setValue(
      "tools.enabled",
      TOOL_NAMES.filter((t) => next.has(t)),
      { shouldDirty: true },
    );
  }, [catalog, getValues, setValue]);

  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-500">Functions the agent can call during a call.</p>
      {isError ? (
        <p className="text-xs font-medium text-red-700">
          Could not load tool catalog — please refresh.
        </p>
      ) : null}
      <Controller
        control={form.control}
        name="tools.enabled"
        render={({ field }) => {
          const enabled = new Set(field.value);
          function toggle(tool: ToolName, on: boolean): void {
            // always_on tools are locked on; never remove them even programmatically.
            if (!on && byName.get(tool)?.always_on === true) return;
            const next = new Set(enabled);
            if (on) next.add(tool);
            else next.delete(tool);
            // Preserve canonical order so diffs stay stable.
            field.onChange(TOOL_NAMES.filter((t) => next.has(t)));
          }
          return (
            <ul className="space-y-2">
              {tools.map(({ name, spec }) => (
                <li
                  key={name}
                  className="flex items-start justify-between gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-card"
                >
                  <label htmlFor={`tool-${name}`} className="min-w-0">
                    <span className="font-mono text-sm text-slate-900">{name}</span>
                    <span className="mt-0.5 block text-xs text-slate-500">{spec.description}</span>
                  </label>
                  <input
                    id={`tool-${name}`}
                    type="checkbox"
                    className="mt-1 h-4 w-4 accent-indigo-600"
                    // always_on tools (end_call) render checked + disabled; the effect
                    // above guarantees they are also present in the submitted value.
                    checked={spec.always_on ? true : enabled.has(name)}
                    disabled={spec.always_on}
                    onChange={(e) => toggle(name, e.target.checked)}
                  />
                </li>
              ))}
            </ul>
          );
        }}
      />
      {isLoading ? <p className="text-xs text-slate-400">Loading tool catalog…</p> : null}
      {error ? <p className="text-xs font-medium text-red-700">{error}</p> : null}
      <SmsTemplatesEditor form={form} />
    </div>
  );
}

// Operator-authored SMS templates for the send_sms tool. The agent never writes free
// text — it picks a template by key — so this is where those bodies live. When send_sms
// is enabled but no templates exist yet, a needs-templates hint warns the operator that
// the agent has nothing to send (the catalog's requires_config flag made the same point
// on the toggle). The body's PHI rejection lives in the zod schema (smsTemplateSchema).
function SmsTemplatesEditor({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const enabled = form.watch("tools.enabled") ?? [];
  const templates = form.watch("tools.sms.templates") ?? [];
  const { fields, append, remove } = useFieldArray({
    control: form.control,
    name: "tools.sms.templates",
  });
  const sendSmsEnabled = enabled.includes("send_sms");
  const needsTemplates = sendSmsEnabled && templates.length === 0;

  return (
    <div className="space-y-3 rounded-xl border border-slate-200 bg-white p-4 shadow-card">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-900">SMS templates</h3>
        <button
          type="button"
          className="rounded-lg border border-slate-300 px-2 py-1 text-xs"
          onClick={() => append({ key: "", label: "", body: "" })}
        >
          Add template
        </button>
      </div>
      {needsTemplates ? (
        <p className="text-xs font-medium text-amber-700">
          send_sms is enabled — needs templates: add at least one template or the agent
          cannot send any text.
        </p>
      ) : null}
      <ul className="space-y-3">
        {fields.map((f, i) => (
          <li key={f.id} className="space-y-2 rounded-lg border border-slate-100 p-3">
            <input
              className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
              placeholder="key (lowercase slug)"
              {...form.register(`tools.sms.templates.${i}.key` as const)}
            />
            <input
              className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
              placeholder="label"
              {...form.register(`tools.sms.templates.${i}.label` as const)}
            />
            <textarea
              className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
              placeholder="body (non-PHI {{variables}} only)"
              {...form.register(`tools.sms.templates.${i}.body` as const)}
            />
            {form.formState.errors.tools?.sms?.templates?.[i]?.body ? (
              <p className="text-xs font-medium text-red-700">
                {form.formState.errors.tools.sms.templates[i]?.body?.message}
              </p>
            ) : null}
            <button
              type="button"
              className="text-xs text-red-700"
              onClick={() => remove(i)}
            >
              Remove
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
