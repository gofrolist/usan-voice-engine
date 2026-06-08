import { Suspense, lazy } from "react";
import { Textarea } from "../../../components/ui/textarea";

// Lazy-load Monaco so it is split out of the main bundle and never blocks first
// paint. While it loads (or if it fails) we render a plain <textarea> with the
// same value/onChange contract, so prompts remain fully editable either way.
const MonacoEditor = lazy(async () => {
  const mod = await import("@monaco-editor/react");
  return { default: mod.default };
});

interface PromptEditorProps {
  id: string;
  value: string;
  onChange: (value: string) => void;
  rows?: number;
}

function Fallback({ id, value, onChange, rows = 6 }: PromptEditorProps) {
  return (
    <Textarea id={id} value={value} rows={rows} onChange={(e) => onChange(e.target.value)} />
  );
}

export function PromptEditor(props: PromptEditorProps) {
  const { value, onChange, rows = 6 } = props;
  return (
    <div className="overflow-hidden rounded border border-gray-300">
      <Suspense fallback={<Fallback {...props} />}>
        <MonacoEditor
          height={`${Math.max(rows, 4) * 22}px`}
          defaultLanguage="markdown"
          value={value}
          onChange={(v) => onChange(v ?? "")}
          options={{
            minimap: { enabled: false },
            lineNumbers: "off",
            wordWrap: "on",
            fontSize: 13,
            scrollBeyondLastLine: false,
            renderLineHighlight: "none",
            folding: false,
          }}
        />
      </Suspense>
    </div>
  );
}
