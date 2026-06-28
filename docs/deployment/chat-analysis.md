# Chat Analysis (Phase 4c-2) — operator note

`PUT /rerun-chat-analysis/{chat_id}` recomputes a chat's post-chat analysis (chat_summary,
user_sentiment, chat_successful) via Vertex and returns the updated chat. It also surfaces
`chat_analysis` on get-chat / list-chats.

**Ships inert.** No analysis runs until BOTH:

- `CHAT_ANALYSIS_ENABLED=true` (default `false`), AND
- `GCP_PROJECT` is set (the Vertex project; ADC service account on the VM).

Optional: `CHAT_ANALYSIS_MODEL` (default `gemini-2.5-flash`).

With the flag off or `GCP_PROJECT` unset, the endpoint still returns 201 with the chat and
any previously stored analysis — it just performs no recompute (no spend, no PHI egress).

PHI: the transcript is sent ONLY to Vertex via ADC (BAA-covered), never the Gemini Developer
API; the analysis persists only to BAA Postgres (`chat_analyses`, RLS-isolated per org).

Migration `0046` (the `chat_analyses` table) must be applied (owner-DDL) on deploy.
