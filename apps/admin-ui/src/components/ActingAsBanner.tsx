import { Link } from "react-router-dom";
import { useSession } from "../auth/useSession";
import { useSwitchOrg } from "../features/orgs/hooks";

// Shown only while a super-admin is acting as another org. "Exit" returns to the
// caller's first home org; a super-admin with no membership (rare — bootstrap
// grants one) instead gets a link back to the org console to pick a target.
export function ActingAsBanner() {
  const { data: me } = useSession();
  const switchOrg = useSwitchOrg();

  if (!me?.acting_as || !me.active_org) return null;

  const homeOrg = me.orgs[0];

  return (
    <div
      role="status"
      className="flex flex-wrap items-center justify-between gap-2 border-b border-amber-300 bg-amber-100 px-4 py-2 text-sm text-amber-900"
    >
      <span>
        Acting as <strong>{me.active_org.name}</strong>
      </span>
      {homeOrg ? (
        <button
          type="button"
          disabled={switchOrg.isPending}
          onClick={() => switchOrg.mutate({ organization_id: homeOrg.id })}
          className="rounded-md border border-amber-400 bg-amber-50 px-2.5 py-1 text-xs font-medium text-amber-900 transition-colors hover:bg-amber-200 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500"
        >
          Exit act-as
        </button>
      ) : (
        <Link
          to="/organizations"
          className="rounded-md border border-amber-400 bg-amber-50 px-2.5 py-1 text-xs font-medium text-amber-900 transition-colors hover:bg-amber-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500"
        >
          Exit act-as
        </Link>
      )}
    </div>
  );
}
