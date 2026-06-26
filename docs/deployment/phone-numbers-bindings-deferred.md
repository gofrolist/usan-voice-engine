# Phone-number bindings: persisted, not yet honored

The compat phone-number surface (`import`/`get`/`update`/`list`/`delete`) persists agent
bindings (`inbound_agents`, `outbound_agents`, `inbound_sms_agents`, `outbound_sms_agents`)
and echoes them back truthfully, but **does not yet honor them at call-routing time**.

Why: the runtime call-plane is single-org and outbound dial uses a single global caller-id
(`settings.telnyx_caller_id`) with a process-wide, org-blind trunk cache; inbound routing is
runtime-only LiveKit SIP dispatch state. Honoring per-number bindings means rewiring the live
outbound dial path and adding an inbound DID→agent map — a change to live-call routing that is
gated on the multi-org call-plane and a concrete client. No endpoint fakes routing: a bound
number returns 200 and stores the binding, and that is all it claims to do.

`create-phone-number` is a documented 501: it requires purchasing a DID via the Telnyx Numbers
API (no client, no key, real spend) — a separate future phase.

Known surface-wide deviations (not Phase 2's to fix; see the design spec §13): not-found
returns the house `404` (oracle declares `422`), and the error envelope `status` is the int
HTTP code (oracle declares the string `"error"`).
