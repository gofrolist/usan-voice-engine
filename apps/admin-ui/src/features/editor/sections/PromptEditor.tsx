import { Suspense, lazy, useRef } from "react";
import type { EditorProps, OnChange, OnMount } from "@monaco-editor/react";
import { ErrorBoundary } from "../../../components/ErrorBoundary";
import { Textarea } from "../../../components/ui/textarea";
import { matchPromptTokens } from "./promptTokens";

// Lazy-load Monaco so it is split out of the main bundle and never blocks first
// paint. While it loads we render a plain <textarea>; if the chunk fails to load
// (e.g. a stale deploy 404s the split chunk) the ErrorBoundary below renders the
// same <textarea> too — Suspense alone only covers the pending state, not a rejected
// import — so prompts remain fully editable either way.
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

type EditorInstance = Parameters<OnMount>[0];
type MonacoInstance = Parameters<OnMount>[1];
type DecorationsCollection = ReturnType<EditorInstance["createDecorationsCollection"]>;
type Decorations = NonNullable<Parameters<EditorInstance["createDecorationsCollection"]>[0]>;

const MONACO_OPTIONS: EditorProps["options"] = {
  minimap: { enabled: false },
  lineNumbers: "off",
  wordWrap: "on",
  fontSize: 13,
  scrollBeyondLastLine: false,
  renderLineHighlight: "none",
  folding: false,
  padding: { top: 10, bottom: 10 },
};

function Fallback({ id, value, onChange, rows = 6 }: PromptEditorProps) {
  return (
    <Textarea id={id} value={value} rows={rows} onChange={(e) => onChange(e.target.value)} />
  );
}

export function PromptEditor(props: PromptEditorProps) {
  const { value, onChange, rows = 6 } = props;
  const editorRef = useRef<EditorInstance | null>(null);
  const monacoRef = useRef<MonacoInstance | null>(null);
  const collectionRef = useRef<DecorationsCollection | null>(null);

  // Tint {{variable}} tokens so migrated Retell prompts read well. matchPromptTokens is
  // linear/backtrack-free (see promptTokens.ts). The decorations collection is owned by
  // the editor and torn down with it (@monaco-editor/react disposes it on unmount), so
  // no manual cleanup is required.
  function highlightTokens(): void {
    const editor = editorRef.current;
    const monaco = monacoRef.current;
    const collection = collectionRef.current;
    if (!editor || !monaco || !collection) return;
    const model = editor.getModel();
    if (!model) return;
    const decorations: Decorations = matchPromptTokens(model.getValue()).map((tok) => {
      const start = model.getPositionAt(tok.start);
      const end = model.getPositionAt(tok.end);
      return {
        range: new monaco.Range(start.lineNumber, start.column, end.lineNumber, end.column),
        options: { inlineClassName: "prompt-var-token" },
      };
    });
    collection.set(decorations);
  }

  const handleMount: OnMount = (editor, monaco) => {
    editorRef.current = editor;
    monacoRef.current = monaco;
    collectionRef.current = editor.createDecorationsCollection();
    highlightTokens();
  };

  const handleChange: OnChange = (v) => {
    onChange(v ?? "");
    highlightTokens();
  };

  return (
    <div className="overflow-hidden rounded-lg border border-slate-300">
      <ErrorBoundary fallback={<Fallback {...props} />}>
        <Suspense fallback={<Fallback {...props} />}>
          <MonacoEditor
            height={`${Math.max(rows, 4) * 22}px`}
            defaultLanguage="markdown"
            value={value}
            onChange={handleChange}
            onMount={handleMount}
            options={MONACO_OPTIONS}
          />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
