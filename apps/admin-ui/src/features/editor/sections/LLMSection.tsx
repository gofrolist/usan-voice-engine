import { Controller, type UseFormReturn } from "react-hook-form";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { groupByKind, useModelCatalog, type ModelSpec } from "../../../config/modelCatalog";
import { Select } from "../../../components/ui/select";
import { Field } from "./Field";
import { NumberControl } from "./controls";

// US2 / FR-011, FR-013: a curated LLM-model select. The value stays llm.model (a plain
// string — never a Zod enum), so a published config referencing a withdrawn model still
// loads. The select offers only ACTIVE catalog models for new selection, but always
// includes the CURRENTLY-selected value (even if deprecated/out-of-catalog) so loading a
// published config never silently switches models; a deprecation marker explains it.
function llmOptions(models: ModelSpec[], current: string): ModelSpec[] {
  const active = models.filter((m) => !m.deprecated);
  if (current && !active.some((m) => m.id === current)) {
    // Keep the current value selectable. Reuse its catalog spec (deprecated) if present,
    // otherwise synthesize a minimal entry for an id that left the catalog entirely.
    const spec =
      models.find((m) => m.id === current) ??
      ({
        id: current,
        label: `${current} (not in catalog)`,
        description: "",
        kind: "llm",
        provider: "",
        deprecated: true,
        default: false,
      } satisfies ModelSpec);
    return [spec, ...active];
  }
  return active;
}

export function LLMSection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const errors = form.formState.errors.llm;
  const { data: models, isError, isLoading } = useModelCatalog();
  const { llm } = groupByKind(models);
  const current = form.watch("llm.model") ?? "";
  const options = llmOptions(llm, current);
  const currentSpec = (models ?? []).find((m) => m.id === current);
  const currentDeprecated = Boolean(current) && (currentSpec?.deprecated ?? !currentSpec);

  return (
    <div className="space-y-5">
      <Field path="llm.model" error={errors?.model?.message}>
        <Controller
          control={form.control}
          name="llm.model"
          render={({ field }) => (
            <Select
              id="llm.model"
              value={field.value ?? ""}
              onChange={(e) => field.onChange(e.target.value)}
              onBlur={field.onBlur}
            >
              {options.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                  {m.deprecated ? " (deprecated)" : ""}
                </option>
              ))}
            </Select>
          )}
        />
        {isError ? (
          <p className="mt-1 text-xs font-medium text-red-700">
            Could not load model catalog — please refresh.
          </p>
        ) : null}
        {isLoading ? <p className="mt-1 text-xs text-slate-400">Loading model catalog…</p> : null}
        {currentDeprecated ? (
          <p className="mt-1 text-xs font-medium text-amber-700">
            This model is deprecated. Published calls keep using it until you pick a current model.
          </p>
        ) : null}
      </Field>
      <Field path="llm.temperature" error={errors?.temperature?.message}>
        <NumberControl
          control={form.control}
          name="llm.temperature"
          id="llm.temperature"
          nullable
          step="0.1"
          min={0}
          max={2}
        />
      </Field>
    </div>
  );
}
