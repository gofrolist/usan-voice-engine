import { useEffect, useRef, useState } from "react";
import {
  type RemoteTrack,
  type RemoteTrackPublication,
  type RemoteParticipant,
  Room,
  RoomEvent,
  Track,
} from "livekit-client";
import { ApiError, testProfileAudio } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type { AgentConfig } from "../../types/api";

interface TestAudioPanelProps {
  profileId: string;
  // The LIVE form config to test (unsaved edits included); null -> stored draft.
  getConfig: () => AgentConfig | null;
}

type Status = "idle" | "connecting" | "connected" | "ended";

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

export function TestAudioPanel({ profileId, getConfig }: TestAudioPanelProps) {
  const [status, setStatus] = useState<Status>("idle");
  const [sampleVarsRaw, setSampleVarsRaw] = useState("");
  const roomRef = useRef<Room | null>(null);
  const audioElRef = useRef<HTMLAudioElement | null>(null);

  // Always tear the room down on unmount so a navigated-away test call cannot keep
  // the mic open or hold the throwaway room.
  useEffect(() => {
    return () => {
      void roomRef.current?.disconnect();
      roomRef.current = null;
    };
  }, []);

  async function onStart(): Promise<void> {
    if (status === "connecting" || status === "connected") return;
    setStatus("connecting");
    try {
      const { url, token } = await testProfileAudio(profileId, {
        sample_vars: parseSampleVars(sampleVarsRaw),
        config: getConfig(),
      });
      const room = new Room();
      roomRef.current = room;
      // Play the agent's subscribed audio track through a single <audio> element.
      room.on(
        RoomEvent.TrackSubscribed,
        (track: RemoteTrack, _pub: RemoteTrackPublication, _p: RemoteParticipant) => {
          if (track.kind === Track.Kind.Audio && audioElRef.current) {
            track.attach(audioElRef.current);
          }
        },
      );
      room.on(RoomEvent.Disconnected, () => setStatus("ended"));
      await room.connect(url, token);
      // Publish the operator's mic so they can speak to the agent.
      await room.localParticipant.setMicrophoneEnabled(true);
      setStatus("connected");
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : "Could not start the test call.";
      pushToast(detail);
      setStatus("idle");
      void roomRef.current?.disconnect();
      roomRef.current = null;
    }
  }

  async function onEnd(): Promise<void> {
    await roomRef.current?.disconnect();
    roomRef.current = null;
    setStatus("ended");
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-slate-500">
        Place a browser test call to the draft agent — no phone number is used and no call
        record is created. Allow microphone access when prompted. Supply synthetic{" "}
        <code>name=value</code> sample variables below (one per line).
      </p>
      <textarea
        aria-label="Sample variables"
        className="h-16 w-full rounded border border-slate-300 p-2 font-mono text-xs"
        placeholder={"first_name=Alex\ncompany=Example"}
        value={sampleVarsRaw}
        onChange={(e) => setSampleVarsRaw(e.target.value)}
        disabled={status === "connected" || status === "connecting"}
      />
      <div className="flex items-center gap-3">
        {status === "connected" ? (
          <button
            type="button"
            className="rounded bg-red-600 px-3 py-1 text-sm font-medium text-white hover:bg-red-700"
            onClick={() => void onEnd()}
          >
            End call
          </button>
        ) : (
          <button
            type="button"
            className="rounded bg-sky-600 px-3 py-1 text-sm font-medium text-white hover:bg-sky-700 disabled:opacity-50"
            disabled={status === "connecting"}
            onClick={() => void onStart()}
          >
            {status === "connecting" ? "Connecting…" : "Start test call"}
          </button>
        )}
        <span aria-live="polite" className="text-sm text-slate-600">
          {status === "idle"
            ? "Ready"
            : status === "connecting"
              ? "Connecting…"
              : status === "connected"
                ? "Connected — speak to the agent"
                : "Call ended"}
        </span>
      </div>
      {/* The agent's audio is attached here on TrackSubscribed. */}
      <audio ref={audioElRef} autoPlay />
    </div>
  );
}
