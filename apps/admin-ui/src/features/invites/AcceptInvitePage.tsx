import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";

const REASONS: Record<string, string> = {
  mismatch:
    "This invitation was issued to a different email address. Sign in with the address it was sent to.",
  expired: "This invitation has expired. Ask an organization admin to send a new one.",
  revoked: "This invitation has been revoked.",
  invalid: "This invitation link is not valid.",
};

// Public, unauthenticated. With ?token it forwards into the API accept endpoint
// (which bounces through Google). With ?status=error it shows a friendly message.
export function AcceptInvitePage() {
  const [params] = useSearchParams();
  const status = params.get("status");
  const token = params.get("token");

  useEffect(() => {
    if (status !== "error" && token) {
      window.location.assign(`/v1/auth/accept-invite?token=${encodeURIComponent(token)}`);
    }
  }, [status, token]);

  if (status === "error") {
    const reason = params.get("reason") ?? "invalid";
    return (
      <div className="flex h-screen items-center justify-center p-6">
        <div className="max-w-md text-center text-slate-700">
          <p className="font-medium">Can't accept this invitation</p>
          <p className="mt-1 text-sm text-slate-500">{REASONS[reason] ?? REASONS.invalid}</p>
          <a className="mt-4 inline-block text-sm text-blue-700 underline" href="/v1/auth/login">
            Go to sign in
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen items-center justify-center">
      <span className="text-slate-600">Redirecting to sign in…</span>
    </div>
  );
}
