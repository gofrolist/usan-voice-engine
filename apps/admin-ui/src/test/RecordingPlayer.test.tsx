// apps/admin-ui/src/test/RecordingPlayer.test.tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { RecordingPlayer } from "../features/calls/RecordingPlayer";

const SIGNED_URL = "https://storage.example/recording.ogg?sig=SIGNED-SENTINEL";

describe("RecordingPlayer", () => {
  it("renders a native audio player with the signed URL and a TTL note", () => {
    const { container } = render(
      <RecordingPlayer url={SIGNED_URL} ttlS={600} hasRecording={true} callStatus="completed" />,
    );

    const audio = container.querySelector("audio");
    expect(audio).not.toBeNull();
    expect(audio).toHaveAttribute("controls");
    expect(audio).toHaveAttribute("preload", "none");
    expect(audio).toHaveAttribute("src", SIGNED_URL);
    expect(
      screen.getByText("Recording link expires in ~10 min — reload the page for a fresh link."),
    ).toBeInTheDocument();
  });

  it("never renders the bearer URL as text — only as the audio src", () => {
    const { container } = render(
      <RecordingPlayer url={SIGNED_URL} ttlS={600} hasRecording={true} callStatus="completed" />,
    );

    // The URL is a bearer secret: it must live only in the src attribute.
    expect(container.textContent).not.toContain("SIGNED-SENTINEL");
    expect(container.textContent).not.toContain("storage.example");
  });

  it("shows the generic no-link copy when a recording exists but no URL was signed", () => {
    // Deliberately generic — null covers signing failure AND an unconfigured bucket.
    const { container } = render(
      <RecordingPlayer url={null} ttlS={null} hasRecording={true} callStatus="completed" />,
    );

    expect(
      screen.getByText("Recording exists but no playback link is available right now."),
    ).toBeInTheDocument();
    expect(container.querySelector("audio")).toBeNull();
  });

  it("shows the in-progress copy for a non-terminal call", () => {
    render(<RecordingPlayer url={null} ttlS={null} hasRecording={false} callStatus="in_progress" />);

    expect(
      screen.getByText("Call still in progress — recording appears after the call ends."),
    ).toBeInTheDocument();
  });

  it("shows the no-recording copy for a terminal call without a recording", () => {
    render(<RecordingPlayer url={null} ttlS={null} hasRecording={false} callStatus="completed" />);

    expect(screen.getByText("No recording for this call.")).toBeInTheDocument();
  });
});
