import { useState } from "react";
import { ApiError, testProfileLlm } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type { AgentConfig, TestMessage, TestToolCall } from "../../types/api";

interface TestLLMPanelProps {
  profileId: string;
  // Returns the config to test — the LIVE form values, so unsaved draft edits are
  // exercised. Omit (return null) to let the server use the stored draft.
  getConfig: () => AgentConfig | null;
}

interface TranscriptTurn {
  role: "user" | "assistant";
  content: string;
  toolCalls?: TestToolCall[];
}

// Render one synthetic {{name}}=value sample-var row. Parsed from a small textarea
// so an admin can supply PHI-free placeholder values without any real contact data.
function parseSampleVars(raw: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of raw.split("\n")) {
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    const name = line.slice(0, eq).trim();
    const value = line.slice(eq + 1).trim();
    if (name) out[name] = value;
  }
  return out;
}

export function TestLLMPanel({ profileId, getConfig }: TestLLMPanelProps) {
  const [input, setInput] = useState("");
  const [sampleVarsRaw, setSampleVarsRaw] = useState("");
  const [turns, setTurns] = useState<TranscriptTurn[]>([]);
  const [busy, setBusy] = useState(false);

  async function onSend(): Promise<void> {
    const content = input.trim();
    if (!content || busy) return;
    const nextTurns: TranscriptTurn[] = [...turns, { role: "user", content }];
    setTurns(nextTurns);
    setInput("");
    setBusy(true);
    try {
      const messages: TestMessage[] = nextTurns.map((t) => ({
        role: t.role,
        content: t.content,
      }));
      const res = await testProfileLlm(profileId, {
        messages,
        sample_vars: parseSampleVars(sampleVarsRaw),
        config: getConfig(),
      });
      setTurns((prev) => [
        ...prev,
        { role: "assistant", content: res.assistant, toolCalls: res.tool_calls },
      ]);
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : "Test failed.";
      pushToast(detail);
      // Roll the failed user turn back so the transcript stays consistent.
      setTurns((prev) => prev.slice(0, -1));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-slate-500">
        Simulate the conversation against the draft prompt and tools. Tools are stubbed —
        nothing is saved and no real contact data is used. Supply synthetic{" "}
        <code>name=value</code> sample variables below (one per line).
      </p>
      <textarea
        aria-label="Sample variables"
        className="h-16 w-full rounded border border-slate-300 p-2 font-mono text-xs"
        placeholder={"first_name=Alex\ncompany=Example"}
        value={sampleVarsRaw}
        onChange={(e) => setSampleVarsRaw(e.target.value)}
      />
      <div
        role="log"
        aria-label="Test transcript"
        className="flex min-h-[8rem] flex-col gap-2 rounded border border-slate-200 bg-slate-50 p-3 text-sm"
      >
        {turns.length === 0 ? (
          <span className="text-slate-400">No messages yet. Send one to start.</span>
        ) : (
          turns.map((t, i) => (
            <div key={i} className={t.role === "user" ? "text-slate-900" : "text-sky-800"}>
              <span className="font-semibold">{t.role === "user" ? "You" : "Agent"}:</span>{" "}
              {t.content}
              {t.toolCalls && t.toolCalls.length > 0 ? (
                <ul className="mt-1 list-disc pl-5 text-xs text-amber-700">
                  {t.toolCalls.map((tc, j) => (
                    <li key={j}>
                      tool: <code>{tc.name}</code>({JSON.stringify(tc.args)})
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ))
        )}
      </div>
      <div className="flex gap-2">
        <input
          aria-label="Your message"
          className="flex-1 rounded border border-slate-300 px-2 py-1 text-sm"
          placeholder="Type a message…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void onSend();
          }}
        />
        <button
          type="button"
          className="rounded bg-sky-600 px-3 py-1 text-sm font-medium text-white hover:bg-sky-700 disabled:opacity-50"
          disabled={busy || input.trim().length === 0}
          onClick={() => void onSend()}
        >
          {busy ? "Sending…" : "Send"}
        </button>
      </div>
    </div>
  );
}
