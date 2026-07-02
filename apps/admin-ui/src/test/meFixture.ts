import type { AdminUserRole, Me } from "../types/api";

// Test helper: a full Me for a regular (non-super-admin) member whose active org
// grants the given role. Mirrors the P2 /v1/auth/me shape so useIsAdmin() gates
// correctly in component tests.
export function meFixture(role: AdminUserRole, over: Partial<Me> = {}): Me {
  return {
    email: "me@example.com",
    is_super_admin: false,
    acting_as: false,
    version: "dev",
    active_org: {
      id: "00000000-0000-0000-0000-0000000000a1",
      name: "Acme Care",
      slug: "acme-care",
      role,
    },
    orgs: [
      {
        id: "00000000-0000-0000-0000-0000000000a1",
        name: "Acme Care",
        slug: "acme-care",
        role,
      },
    ],
    ...over,
  };
}
