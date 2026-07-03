import { Controller, type UseFormReturn } from "react-hook-form";
import { Link } from "react-router-dom";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { useKnowledgeBases } from "../../knowledgeBases/hooks";

// Binds knowledge bases to this agent. The config stores encoded knowledge_base_<hex>
// tokens (KbSummary.agent_ref); this section toggles those tokens in
// llm.knowledge_base_ids. A bound token with no matching KB in the org list (deleted or
// compat-created) is surfaced as an amber "Unknown knowledge base" row with a Remove
// button, and PRESERVED on every edit — never silently dropped. Applied on Publish, like
// every other editor field.
export function KnowledgeBaseSection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const { data: kbs, isLoading, isError } = useKnowledgeBases();

  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-500">
        Knowledge bases the agent can retrieve from during a call or chat. Bindings apply when
        you publish.
      </p>
      {isError ? (
        <p className="text-xs font-medium text-red-700">
          Could not load knowledge bases — bound ones below are still shown from this draft.
        </p>
      ) : null}
      <Controller
        control={form.control}
        name="llm.knowledge_base_ids"
        render={({ field }) => {
          const bound = new Set(field.value ?? []);
          const list = kbs ?? [];
          const knownRefs = new Set(list.map((k) => k.agent_ref));
          const orphans = [...bound].filter((ref) => !knownRefs.has(ref));

          function toggle(ref: string, on: boolean): void {
            const next = new Set(bound);
            if (on) next.add(ref);
            else next.delete(ref);
            field.onChange([...next]);
          }

          if (!isLoading && !isError && list.length === 0 && orphans.length === 0) {
            return (
              <p className="text-sm text-slate-500">
                No knowledge bases yet —{" "}
                <Link className="text-accent hover:underline" to="/knowledge-bases">
                  create one under Knowledge
                </Link>
                .
              </p>
            );
          }

          return (
            <ul className="space-y-2">
              {list.map((kb) => (
                <li
                  key={kb.agent_ref}
                  className="flex items-start justify-between gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-card"
                >
                  <label htmlFor={`kb-${kb.agent_ref}`} className="min-w-0">
                    <span className="text-sm text-slate-900">{kb.name}</span>
                    <span className="mt-0.5 block text-xs text-slate-500">
                      {kb.status === "complete"
                        ? `${kb.source_count} source${kb.source_count === 1 ? "" : "s"}`
                        : kb.status.replace("_", " ")}
                    </span>
                  </label>
                  <input
                    id={`kb-${kb.agent_ref}`}
                    type="checkbox"
                    className="mt-1 h-4 w-4 accent-indigo-600"
                    checked={bound.has(kb.agent_ref)}
                    onChange={(e) => toggle(kb.agent_ref, e.target.checked)}
                  />
                </li>
              ))}
              {orphans.map((ref) => (
                <li
                  key={ref}
                  className="flex items-start justify-between gap-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3"
                >
                  <div className="min-w-0">
                    <span className="text-sm text-slate-900">Unknown knowledge base</span>
                    <span className="mt-0.5 block break-all font-mono text-xs text-slate-500">
                      {ref}
                    </span>
                  </div>
                  <button
                    type="button"
                    className="mt-0.5 text-xs font-medium text-red-700"
                    onClick={() => toggle(ref, false)}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          );
        }}
      />
      {isLoading ? <p className="text-xs text-slate-400">Loading knowledge bases…</p> : null}
    </div>
  );
}
