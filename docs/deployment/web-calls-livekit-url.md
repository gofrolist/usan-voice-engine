# Web Calls — LiveKit server URL (client-side integration)

Phase 3 serves `POST /v2/create-web-call` (RetellAI-compatible). The response carries a
real, working LiveKit WebRTC `access_token`, and our agent answers the browser
participant. The REST contract is fully oracle-conformant.

## The one caveat: the token does not carry a server URL

The minted `access_token` encodes the **room** and **participant identity**, not the
LiveKit **server URL**. RetellAI's `RetellWebClient` connects to *RetellAI's* LiveKit
cloud by default. To join a call created against this engine, the client's frontend must
connect to **our** `LIVEKIT_URL` (the `wss://…` the deployment exposes) using the minted
token — e.g. raw `livekit-client`:

    import { Room } from "livekit-client";
    const room = new Room();
    await room.connect(USAN_LIVEKIT_WSS_URL, accessToken);

A true zero-change repoint of a RetellAI **browser** SDK that hardcodes the RetellAI
server URL is therefore not possible from the token alone — the client must point at our
`LIVEKIT_URL`. (Same class of edge-integration caveat as outbound webhook delivery: the
REST surface is drop-in; one client-side endpoint must target our infrastructure.)

## Operator checklist

- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` must be set (already required by
  the existing outbound/test-audio paths — no new env keys).
- Migration `0041` (the `call_type` column) must be applied — it ships with the `v*` tag
  deploy and runs as the `usan` table owner.
- No master enable flag: like the rest of the compat surface, `create-web-call` 401s
  until a super-admin mints a compat key.
