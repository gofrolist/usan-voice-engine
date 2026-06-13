import { Controller, type UseFormReturn } from "react-hook-form";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { groupByKind, useModelCatalog, type ModelSpec } from "../../../config/modelCatalog";
import { Select } from "../../../components/ui/select";
import { Field } from "./Field";
import { TextControl } from "./controls";

// US2 / FR-012: a curated STT-model select. The value stays stt.model (a plain string —
// never a Zod enum), so a published config referencing a withdrawn model still loads. The
// select offers only ACTIVE catalog models for new selection, but always includes the
// CURRENTLY-selected value (even if deprecated/out-of-catalog) so loading a published
// config never silently switches models; a deprecation marker explains it.
function sttOptions(models: ModelSpec[], current: string): ModelSpec[] {
  const active = models.filter((m) => !m.deprecated);
  if (current && !active.some((m) => m.id === current)) {
    const spec =
      models.find((m) => m.id === current) ??
      ({
        id: current,
        label: `${current} (not in catalog)`,
        description: "",
        kind: "stt",
        provider: "",
        deprecated: true,
        default: false,
      } satisfies ModelSpec);
    return [spec, ...active];
  }
  return active;
}

export function STTSection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const errors = form.formState.errors.stt;
  const { data: models, isError, isLoading } = useModelCatalog();
  const { stt } = groupByKind(models);
  const current = form.watch("stt.model") ?? "";
  const options = sttOptions(stt, current);
  const currentSpec = (models ?? []).find((m) => m.id === current);
  const currentDeprecated = Boolean(current) && (currentSpec?.deprecated ?? !currentSpec);

  return (
    <div className="space-y-5">
      <Field path="stt.model" error={errors?.model?.message}>
        <Controller
          control={form.control}
          name="stt.model"
          render={({ field }) => (
            <Select
              id="stt.model"
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
      <Field path="stt.language" error={errors?.language?.message}>
        <TextControl
          control={form.control}
          name="stt.language"
          id="stt.language"
          nullable
          placeholder="(plugin default)"
        />
      </Field>
    </div>
  );
}
