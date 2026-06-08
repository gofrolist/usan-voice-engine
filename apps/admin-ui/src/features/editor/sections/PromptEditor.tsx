import { Suspense, lazy, useRef } from "react";
import type { EditorProps, OnChange, OnMount } from "@monaco-editor/react";
import { ErrorBoundary } from "../../../components/ErrorBoundary";
import { Textarea } from "../../../components/ui/textarea";

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

// {{variable}} / {slot} tokens. Both arms use [^{}] so a token can never span an
// unmatched brace — this also avoids quadratic backtracking on pathological input
// (e.g. a long run of "{" with no closer).
const TOKEN_RE = /\{\{[^{}]+\}\}|\{[^{}]+\}/g;

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

  // Tint {{variable}} tokens so migrated Retell prompts read well. The decorations
  // collection is owned by the editor and torn down with it (@monaco-editor/react
  // disposes the editor on unmount), so no manual cleanup is required.
  function highlightTokens(): void {
    const editor = editorRef.current;
    const monaco = monacoRef.current;
    const collection = collectionRef.current;
    if (!editor || !monaco || !collection) return;
    const model = editor.getModel();
    if (!model) return;
    const text = model.getValue();
    const decorations: Decorations = [];
    TOKEN_RE.lastIndex = 0; // shared global regex — reset before each scan
    let m: RegExpExecArray | null;
    while ((m = TOKEN_RE.exec(text)) !== null) {
      const start = model.getPositionAt(m.index);
      const end = model.getPositionAt(m.index + m[0].length);
      decorations.push({
        range: new monaco.Range(start.lineNumber, start.column, end.lineNumber, end.column),
        options: { inlineClassName: "prompt-var-token" },
      });
    }
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
