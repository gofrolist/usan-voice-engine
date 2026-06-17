import { useSession } from "../auth/useSession";
import { useOrganizations, useSwitchOrg } from "../features/orgs/hooks";
import { Select } from "./ui/select";

interface OrgOption {
  id: string;
  name: string;
}

// Footer org switcher. A regular member sees only the orgs they belong to; a
// super-admin additionally sees every org from /v1/admin/organizations as an
// "act as" target (de-duped against their memberships). The currently active org
// is always present in the list even when it's an act-as org outside me.orgs.
export function OrgSwitcher() {
  const { data: me } = useSession();
  const switchOrg = useSwitchOrg();
  // Only super-admins may list all orgs; the query stays disabled otherwise (403).
  const orgs = useOrganizations(!!me?.is_super_admin);

  if (!me) return null;

  const options = new Map<string, OrgOption>();
  for (const o of me.orgs) options.set(o.id, { id: o.id, name: o.name });
  if (me.is_super_admin) {
    for (const o of orgs.data ?? []) {
      if (!options.has(o.id)) options.set(o.id, { id: o.id, name: o.name });
    }
  }
  // Guarantee the active org is selectable (e.g. an act-as org not yet in either list).
  if (me.active_org && !options.has(me.active_org.id)) {
    options.set(me.active_org.id, { id: me.active_org.id, name: me.active_org.name });
  }

  const activeId = me.active_org?.id ?? "";

  function onChange(id: string): void {
    if (id === activeId) return;
    switchOrg.mutate({ organization_id: id });
  }

  return (
    <div className="mb-2.5">
      <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-faint">
        Organization
      </label>
      <Select
        aria-label="Switch organization"
        value={activeId}
        disabled={switchOrg.isPending}
        onChange={(e) => onChange(e.target.value)}
      >
        {[...options.values()].map((o) => (
          <option key={o.id} value={o.id}>
            {o.name}
          </option>
        ))}
      </Select>
    </div>
  );
}
