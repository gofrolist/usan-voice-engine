import { type Control, type FieldPath, Controller } from "react-hook-form";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { Input } from "../../../components/ui/input";

type Path = FieldPath<AgentConfigForm>;

interface NumberControlProps {
  control: Control<AgentConfigForm>;
  name: Path;
  id: string;
  // When true the field maps "" → null; otherwise "" → NaN (kept so Zod can flag it).
  nullable: boolean;
  step?: string;
  min?: number;
  max?: number;
}

// Controlled numeric input that round-trips number | null correctly. An empty box
// becomes null for nullable fields (server treats null as "use plugin default").
export function NumberControl({ control, name, id, nullable, step, min, max }: NumberControlProps) {
  return (
    <Controller
      control={control}
      name={name}
      render={({ field }) => {
        const raw = field.value;
        const display = raw === null || raw === undefined || Number.isNaN(raw) ? "" : String(raw);
        return (
          <Input
            id={id}
            type="number"
            inputMode="decimal"
            step={step}
            min={min}
            max={max}
            value={display}
            onChange={(e) => {
              const text = e.target.value;
              if (text === "") {
                field.onChange(nullable ? null : NaN);
                return;
              }
              field.onChange(Number(text));
            }}
            onBlur={field.onBlur}
          />
        );
      }}
    />
  );
}

interface TextControlProps {
  control: Control<AgentConfigForm>;
  name: Path;
  id: string;
  // When true an empty box becomes null instead of "".
  nullable: boolean;
  placeholder?: string;
}

export function TextControl({ control, name, id, nullable, placeholder }: TextControlProps) {
  return (
    <Controller
      control={control}
      name={name}
      render={({ field }) => {
        const value = field.value;
        const display = value === null || value === undefined ? "" : String(value);
        return (
          <Input
            id={id}
            value={display}
            placeholder={placeholder}
            onChange={(e) => {
              const text = e.target.value;
              field.onChange(nullable && text === "" ? null : text);
            }}
            onBlur={field.onBlur}
          />
        );
      }}
    />
  );
}
