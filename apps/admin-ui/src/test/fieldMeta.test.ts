// apps/admin-ui/src/test/fieldMeta.test.ts
import { describe, expect, it } from "vitest";
import { fieldMeta } from "../config/fieldMeta";

describe("fieldMeta prompt help text", () => {
  it("mentions the insert-variable palette on the greeting", () => {
    expect(fieldMeta["prompts.greeting"]!.help).toMatch(/\{\{variable\}\}/);
    expect(fieldMeta["prompts.greeting"]!.help.toLowerCase()).toContain("insert");
  });

  it("explains defaults on the greeting help", () => {
    expect(fieldMeta["prompts.greeting"]!.help.toLowerCase()).toContain("default");
  });

  it("points the personalization template at {{variables}} and legacy slots", () => {
    const help = fieldMeta["prompts.inbound_personalization_template"]!.help;
    expect(help).toMatch(/\{\{variable\}\}/);
    expect(help).toContain("{elder_name}");
    expect(help).toContain("{last_check_in_line}");
  });

  it("every prompt field help mentions {{variables}}", () => {
    const keys = [
      "prompts.system_prompt",
      "prompts.greeting",
      "prompts.recording_disclosure",
      "prompts.voicemail_message",
      "prompts.checkin_flow_instructions",
      "prompts.goodbye_message",
      "prompts.inbound_opening",
      "prompts.inbound_personalization_template",
    ];
    for (const k of keys) {
      expect(fieldMeta[k]!.help).toMatch(/\{\{variable\}\}/);
    }
  });
});

describe("fieldMeta tools help text", () => {
  it("does not hardcode the old four-tool list", () => {
    const help = fieldMeta["tools.enabled"]!.help;
    // The catalog is now the source of truth; help must not enumerate the old set.
    expect(help).not.toContain("log_medication");
    expect(help).not.toContain("get_today_meds");
    expect(help.toLowerCase()).toContain("catalog");
  });
});

describe("fieldMeta tools.sms", () => {
  it("registers tools.sms help mentioning templates and non-PHI", () => {
    const meta = fieldMeta["tools.sms"];
    expect(meta).toBeDefined();
    expect(meta!.label.toLowerCase()).toContain("sms");
    expect(meta!.help.toLowerCase()).toMatch(/template/);
    expect(meta!.help.toLowerCase()).toMatch(/non-phi|protected health|phi/);
  });
});
