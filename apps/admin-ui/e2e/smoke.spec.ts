// Playwright E2E smoke walkthrough: login -> profiles list -> edit -> publish ->
// rollback. AUTHORED ONLY — it runs in CI / Plan P5, never in this package's unit
// suite. Guarded behind E2E=1 so an accidental `playwright test` here is skipped,
// and kept under e2e/ (outside the tsconfig `include` and the vitest test glob) so
// it cannot break typecheck/lint without @playwright/test installed.
//
// To run (CI/P5): `E2E=1 BASE_URL=http://localhost:4173 npx playwright test`.

// eslint-disable-next-line @typescript-eslint/ban-ts-comment
// @ts-nocheck
import { expect, test } from "@playwright/test";

const RUN_E2E = process.env.E2E === "1";
const BASE_URL = process.env.BASE_URL ?? "http://localhost:4173";

// Skip the whole suite unless explicitly enabled — there is no browser/back end
// wired up in P4. P5 provides the served app + a seeded session.
test.describe.skip(!RUN_E2E, () => {
  test.describe("admin-ui smoke", () => {
    test.beforeEach(async ({ page }) => {
      // P5 seeds an authenticated session cookie before navigating; the SPA's api
      // wrapper would otherwise redirect to /v1/auth/login on the first call.
      await page.goto(BASE_URL + "/");
    });

    test("login -> list -> edit -> publish -> rollback", async ({ page }) => {
      // 1. Profiles list renders for an authenticated operator.
      await expect(page.getByRole("heading", { name: /agent profiles/i })).toBeVisible();

      // 2. Open the first profile in the editor.
      const firstRow = page.getByRole("row").nth(1);
      await firstRow.click();
      await expect(page.getByRole("button", { name: /save draft/i })).toBeVisible();

      // 3. Edit the greeting prompt.
      await page.getByRole("tab", { name: /prompts/i }).click();
      const greeting = page.locator("#prompts\\.greeting");
      await greeting.fill("Good morning from the smoke test");
      await page.getByRole("button", { name: /save draft/i }).click();

      // 4. Publish — confirm the diff dialog then publish.
      await page.getByRole("button", { name: /^publish$/i }).click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.getByLabel(/note/i).fill("smoke publish");
      await page
        .getByRole("dialog")
        .getByRole("button", { name: /^publish$/i })
        .click();
      await expect(page.getByRole("dialog")).toBeHidden();

      // 5. Go to version history and roll back to a prior version.
      await page.getByRole("link", { name: /version history/i }).click();
      await expect(page.getByRole("heading", { name: /version history/i })).toBeVisible();
      await page
        .getByRole("row")
        .filter({ hasText: "v1" })
        .getByRole("button", { name: /roll back/i })
        .click();
      await page.getByRole("button", { name: /^roll back$/i }).click();
      await expect(page.getByRole("heading", { name: /version history/i })).toBeVisible();
    });
  });
});
