"""Generate voice-engine agent-profile documents from the client's Retell agents.

Bucket B of the RetellAI → self-hosted migration: take the client's three live Retell
single-prompt agents (Companion / Sales / Inbound, plus the Betty QA tester) and emit a
validated ``AgentConfig`` document per agent, ready to seed + publish into the voice engine
(see ``seed_profiles.py`` and ``README.md``).

Inputs live in the CLIENT repo (``usan-retirement-backend``), which is the canonical home of
the prompts and the Retell custom-function decls:

  prompts/<file>_retell.txt        the full single-prompt agent prompt (21–40 KB)
  retell/<agent>/*.json            the RetellAI general_tools decls (dashboard-export shape)

The tool decls are translated by the SAME code the live compat ingest uses
(``usan_api.compat.tool_translate.translate_general_tools``) so the generated
``external_tools`` are byte-identical to what a real ``create-agent`` call would store.

Run (from ``apps/api``)::

    uv run python scripts/seed_retell_profiles/build_profiles.py \
        --backend-repo ~/gofrolist/usan-retirement-backend

Writes ``scripts/seed_retell_profiles/profiles/<key>.json``. Re-runnable and deterministic.
Each output is validated through ``AgentConfig`` before it is written, so a schema drift
fails the build here rather than at seed time.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from usan_api.compat.tool_translate import translate_general_tools
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig

# The curated Cartesia voice used as the migration default for every "Clara" agent
# (warm, calm, mature — "Sarah - Mindful Woman" from the voice catalog). The Retell agents
# ran ElevenLabs voices, which do not exist on our Cartesia stack, so this is a re-selection
# on our stack, NOT a byte-for-byte port. CONFIRM against the Retell dashboard voice choice.
_CLARA_VOICE_ID = "694f9389-aac1-45b6-b726-9d9369183238"

# Balanced Vertex model as the migration default. Retell ran a frontier model (e.g. GPT-4o);
# gemini-2.5-flash is the closest reasoning/latency trade-off in our catalog. CONFIRM per
# agent against the dashboard (the schema default is the cheaper gemini-3.1-flash-lite).
_DEFAULT_LLM_MODEL = "gemini-2.5-flash"

# Short operational lines the worker speaks OUTSIDE the LLM turn loop (recording disclosure,
# voicemail drop, goodbye, greet-only opener). The client's per-call begin message is dynamic
# (companion passes {{bm_greeting}} via the dispatcher; sales varies its opener in-prompt), so
# these static fields are largely inert on the worker path — we default them to the shipped
# clean copy and flag them for per-agent review in the README rather than guess-extracting.
_D = DEFAULT_AGENT_CONFIG.prompts


# key, display name, description, prompt filename, retell tool subdir (None = no client tools),
# per-agent greet-only opening (kept brace-free for the short-field validator).
_AGENTS: list[dict[str, Any]] = [
    {
        "key": "companion",
        "name": "Clara — Companion (Daily Check-in)",
        "description": "Migrated from Retell 'Clara - Morning Check-in v0.2'. Outbound daily "
        "wellness check-ins (morning + evening branching) and inbound for known contacts.",
        "prompt": "checkin_v0.2_retell.txt",
        "tools": "companion",
        "greeting": "Hello! This is your daily check-in from USAN Retirement. "
        "How are you feeling today?",
    },
    {
        "key": "sales",
        "name": "Clara — Sales",
        "description": "Migrated from Retell 'Clara - Sales v0.1'. Outbound cold-call sales "
        "(trial signup, keypad payment, family capture).",
        "prompt": "sales_clara_v0.1_retell.txt",
        "tools": "sales",
        "greeting": "Hello, thank you for taking my call. This is Clara from USAN Retirement.",
    },
    {
        "key": "inbound",
        "name": "Clara — Inbound",
        "description": "Migrated from Retell 'Clara - Inbound v0.1'. Inbound DID-default agent "
        "for callers dialing USAN.",
        "prompt": "inbound_clara_v0.1_retell.txt",
        "tools": "inbound",
        "greeting": "Thank you for calling USAN Retirement. This is Clara. How can I help you?",
    },
    {
        "key": "betty",
        "name": "Betty — QA Tester",
        "description": "Migrated from Retell 'Betty Tester'. Internal QA/regression agent — "
        "not a production caller.",
        "prompt": "betty_tester_retell.txt",
        "tools": None,
        "greeting": _D.greeting,
    },
]


def _read_prompt(backend: Path, filename: str) -> str:
    text = (backend / "prompts" / filename).read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit(f"empty prompt file: prompts/{filename}")
    return text


def _translate_tools(
    backend: Path, subdir: str | None
) -> tuple[list[dict[str, Any]], bool, list[str]]:
    """Return (external_tools, kb_lookup_present, enable) for the agent's tool decls.

    Reads the raw ``retell/<subdir>/*.json`` decls and runs them through the live compat
    translator, so the result matches a real ingest exactly. ``None`` subdir → no client tools.
    """
    if subdir is None:
        return [], False, []
    decls = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((backend / "retell" / subdir).glob("*.json"))
    ]
    t = translate_general_tools(decls)
    if t.skipped:
        print(f"  ! {subdir}: skipped {len(t.skipped)} decl(s): {t.skipped}", file=sys.stderr)
    return t.external_tools, t.kb_lookup_present, t.enable


def _build_config(
    prompt: str, greeting: str, external_tools: list[dict[str, Any]], enable: list[str]
) -> dict[str, Any]:
    """Assemble a full AgentConfig dict for one migrated agent.

    Prompt-field placement is by WHICH AGENT BUILDER reads the field (services/agent), not a
    hedge:
      - ``checkin_flow_instructions`` — the OUTBOUND conversation agent (build_check_in_agent)
      - ``inbound_personalization_template`` — the INBOUND known-contact agent (build_inbound_agent)
    Both are tool-enabled, so the full Retell single-prompt (which drives tool use) goes into both
    — a profile can serve either direction depending on the contact's assignment.

    ``system_prompt`` is DELIBERATELY the thin default persona, NOT the migrated prompt: it backs
    only the greet-only fallback agent (build_agent, pipeline.py), which registers NO tools. Copying
    the tool-driving prompt here would tell that tool-less agent to call functions it doesn't have
    (an unknown inbound caller would then hear hallucinated tool 'success'). Keep it tool-free.
    """
    return {
        "prompts": {
            # Greet-only fallback (unknown inbound) reads this and has no tools — keep it a thin,
            # tool-free persona. The migrated prompt lives in the two flow fields below.
            "system_prompt": _D.system_prompt,
            "checkin_flow_instructions": prompt,
            "inbound_personalization_template": prompt,
            # Short operational lines — defaulted (see module note); review per agent.
            "greeting": greeting,
            "recording_disclosure": _D.recording_disclosure,
            "voicemail_message": _D.voicemail_message,
            "goodbye_message": _D.goodbye_message,
            "inbound_opening": _D.inbound_opening,
        },
        "voice": {"cartesia_voice_id": _CLARA_VOICE_ID, "language": "en"},
        "llm": {"model": _DEFAULT_LLM_MODEL},
        "stt": {"model": "ink-whisper"},
        "timing": {"answer_timeout_s": 50.0, "max_call_duration_s": 1800},
        # Native Clara builtins are OFF: migrated agents drive the client's own edge functions
        # via external_tools (their DB, their contract), not our native wellness tools. `enable`
        # from the translator (e.g. a Retell built-in end_call type) is unioned in when present.
        "tools": {"enabled": list(enable), "external_tools": external_tools},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--backend-repo",
        type=Path,
        default=Path("~/gofrolist/usan-retirement-backend").expanduser(),
        help="Path to the usan-retirement-backend repo (canonical prompts + retell/ decls).",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "profiles",
        help="Output directory for the generated profile documents.",
    )
    args = ap.parse_args()
    backend: Path = args.backend_repo
    if not (backend / "prompts").is_dir():
        raise SystemExit(f"--backend-repo does not look right (no prompts/): {backend}")
    args.out.mkdir(parents=True, exist_ok=True)

    for spec in _AGENTS:
        prompt = _read_prompt(backend, spec["prompt"])
        external_tools, kb_present, enable = _translate_tools(backend, spec["tools"])
        config = _build_config(prompt, spec["greeting"], external_tools, enable)
        # Validate through the real schema — fail the build on any drift, not at seed time.
        AgentConfig.model_validate(config)
        doc = {
            "key": spec["key"],
            "name": spec["name"],
            "description": spec["description"],
            # Bind the scam-protection / wellbeing KBs after seeding, then set
            # llm.knowledge_base_ids (the client's agents call kb_lookup natively via RAG).
            "kb_lookup_present": kb_present,
            "source_prompt": f"prompts/{spec['prompt']}",
            "config": config,
        }
        out_path = args.out / f"{spec['key']}.json"
        out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(
            f"✓ {spec['key']:<10} {len(external_tools):>2} external tools"
            f"{'  (+kb_lookup → bind a KB)' if kb_present else ''}"
            f"  {len(prompt):>6} B prompt  → {out_path.name}"
        )


if __name__ == "__main__":
    main()
