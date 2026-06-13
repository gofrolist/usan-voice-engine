import { Suspense, lazy, useRef, useState } from "react";
import type { EditorProps, OnChange, OnMount } from "@monaco-editor/react";
import { ErrorBoundary } from "../../../components/ErrorBoundary";
import { Textarea } from "../../../components/ui/textarea";
import { matchPromptTokens } from "./promptTokens";
import { unknownTokenNames } from "./unknownTokens";
import { phiTokenNames, SENSITIVE_PROMPT_FIELDS } from "./phiTokens";
import { VariablePalette } from "./VariablePalette";
import { DeclareVariableDialog } from "../../customVariables/DeclareVariableDialog";
import { useCreateCustomVariable } from "../../customVariables/hooks";
import type { VariableSpec } from "../../../config/variableCatalog";

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
  // The prompt field key (e.g. "greeting"). When supplied alongside phiNames, the
  // editor renders a non-blocking PHI warning for sensitive fields.
  fieldKey?: string;
  value: string;
  onChange: (value: string) => void;
  rows?: number;
  // Catalog variables for the insert palette; knownNames drives unknown-token warnings.
  // Optional so existing callers (and the Fallback) keep compiling before Task 3.6.
  variables?: VariableSpec[];
  knownNames?: ReadonlySet<string>;
  // Set of variable names flagged phi=true in the catalog. When provided together
  // with fieldKey, drives the non-blocking PHI notice for sensitive fields.
  phiNames?: ReadonlySet<string>;
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

// Strip the wrapping braces from a token name (used to know which tokens are unknown).
function isUnknown(tokenText: string, known: ReadonlySet<string>): boolean {
  const m = /^\{\{\s*([a-zA-Z0-9_]+)\s*\}\}$/.exec(tokenText);
  return m != null && m[1] != null ? !known.has(m[1]) : false;
}

// Stable empty set so the unknown-token scan is a no-op when no catalog is supplied
// (avoids a new Set() per render changing identity).
const EMPTY_KNOWN: ReadonlySet<string> = new Set<string>();

export function PromptEditor(props: PromptEditorProps) {
  const { value, onChange, rows = 6, variables, knownNames, fieldKey, phiNames } = props;
  const editorRef = useRef<EditorInstance | null>(null);
  const monacoRef = useRef<MonacoInstance | null>(null);
  const collectionRef = useRef<DecorationsCollection | null>(null);

  const known = knownNames ?? EMPTY_KNOWN;
  const unknown = unknownTokenNames(value, known);

  // Inline declaration (US1): declare an undeclared {{token}} without leaving the
  // editor. Reuses useCreateCustomVariable — its ["variable-catalog"] invalidation
  // refetches knownNames, so the warning + Monaco "unknown" decoration auto-clear.
  const create = useCreateCustomVariable();
  const [declareName, setDeclareName] = useState<string | null>(null);

  function declareAllRemaining(): void {
    // Quick bulk declare: create each undeclared token with an empty definition.
    for (const name of unknown) {
      create.mutate({ name, description: "", example: "", phi: false });
    }
  }

  const isSensitive = fieldKey !== undefined && SENSITIVE_PROMPT_FIELDS.has(fieldKey);
  const phiWarnings =
    isSensitive && phiNames !== undefined ? phiTokenNames(value, phiNames) : [];

  // Tint {{variable}} tokens so migrated Retell prompts read well. Known tokens get the
  // indigo .prompt-var-token; tokens whose name is not in the catalog get the amber
  // .prompt-var-token--unknown. matchPromptTokens is linear/backtrack-free. The
  // decorations collection is owned by the editor and torn down with it.
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
      const cls = isUnknown(tok.text, known) ? "prompt-var-token--unknown" : "prompt-var-token";
      return {
        range: new monaco.Range(start.lineNumber, start.column, end.lineNumber, end.column),
        options: { inlineClassName: cls },
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

  // Insert {{token}} at the Monaco cursor when mounted; otherwise (Monaco still
  // loading / fallback textarea under jsdom) append to the current value so the
  // operator never loses the insert.
  function insertToken(token: string): void {
    const editor = editorRef.current;
    if (editor) {
      const selection = editor.getSelection();
      if (selection) {
        editor.executeEdits("insert-variable", [{ range: selection, text: token }]);
        editor.focus();
        return;
      }
    }
    onChange(value + token);
  }

  return (
    <div className="space-y-1">
      {variables && variables.length > 0 ? (
        <div className="flex justify-end">
          <VariablePalette variables={variables} onInsert={insertToken} />
        </div>
      ) : null}
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
      {unknown.length > 0 ? (
        <div className="space-y-1">
          <p className="text-xs font-medium text-amber-700">
            unknown variable: {unknown.join(", ")} — will resolve to empty unless declared as a
            custom variable.
          </p>
          <div className="flex flex-wrap items-center gap-1">
            {unknown.map((name) => (
              <button
                key={name}
                type="button"
                onClick={() => setDeclareName(name)}
                className="rounded border border-amber-400 bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-900 hover:bg-amber-100"
              >
                Declare <code className="font-mono">{`{{${name}}}`}</code>
              </button>
            ))}
            {unknown.length > 1 ? (
              <button
                type="button"
                onClick={declareAllRemaining}
                disabled={create.isPending}
                className="rounded border border-amber-500 bg-white px-2 py-0.5 text-xs font-medium text-amber-900 hover:bg-amber-100 disabled:opacity-50"
              >
                Declare all remaining
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
      {phiWarnings.length > 0 ? (
        <p className="text-xs font-medium text-amber-700">
          ⚠ {phiWarnings.map((n) => `{{${n}}}`).join(", ")} reveals health information and may be
          spoken before the caller&apos;s identity is confirmed (or to voicemail). Avoid health
          variables in this field.
        </p>
      ) : null}
      {declareName !== null ? (
        <DeclareVariableDialog
          name={declareName}
          busy={create.isPending}
          onCancel={() => setDeclareName(null)}
          onCreate={(body) => create.mutate(body, { onSuccess: () => setDeclareName(null) })}
        />
      ) : null}
    </div>
  );
}
