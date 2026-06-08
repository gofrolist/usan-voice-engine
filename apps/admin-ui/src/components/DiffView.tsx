import type { AgentConfig } from "../types/api";
import { Table, Tbody, Td, Th, Thead, Tr } from "./ui/table";

export type DiffKind = "added" | "removed" | "changed";

export interface DiffRow {
  path: string;
  kind: DiffKind;
  oldValue: string;
  newValue: string;
}

const ABSENT = "—";

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

// Flatten an AgentConfig (or any nested object) to dotted leaf paths. Arrays and
// primitives are leaves; null/undefined collapse to a stable sentinel.
function flatten(value: unknown, prefix: string, out: Map<string, string>): void {
  if (isPlainObject(value)) {
    for (const key of Object.keys(value)) {
      const next = prefix ? `${prefix}.${key}` : key;
      flatten(value[key], next, out);
    }
    return;
  }
  out.set(prefix, stringify(value));
}

function stringify(value: unknown): string {
  if (value === null || value === undefined) return ABSENT;
  if (Array.isArray(value)) return JSON.stringify(value);
  return String(value);
}

// Pure: compute the field-level diff between two configs. Exported for testing.
export function diffConfigs(oldCfg: AgentConfig, newCfg: AgentConfig): DiffRow[] {
  const oldFlat = new Map<string, string>();
  const newFlat = new Map<string, string>();
  flatten(oldCfg, "", oldFlat);
  flatten(newCfg, "", newFlat);

  const paths = [...new Set([...oldFlat.keys(), ...newFlat.keys()])].sort();
  const rows: DiffRow[] = [];
  for (const path of paths) {
    const hasOld = oldFlat.has(path);
    const hasNew = newFlat.has(path);
    const ov = oldFlat.get(path) ?? ABSENT;
    const nv = newFlat.get(path) ?? ABSENT;
    if (hasOld && hasNew) {
      if (ov !== nv) rows.push({ path, kind: "changed", oldValue: ov, newValue: nv });
    } else if (hasNew) {
      rows.push({ path, kind: "added", oldValue: ABSENT, newValue: nv });
    } else {
      rows.push({ path, kind: "removed", oldValue: ov, newValue: ABSENT });
    }
  }
  return rows;
}

export function DiffView({ oldConfig, newConfig }: { oldConfig: AgentConfig; newConfig: AgentConfig }) {
  const rows = diffConfigs(oldConfig, newConfig);
  if (rows.length === 0) {
    return <p className="text-sm text-gray-500">No changes.</p>;
  }
  return (
    <Table>
      <Thead>
        <Tr>
          <Th>Field</Th>
          <Th>Old</Th>
          <Th>New</Th>
        </Tr>
      </Thead>
      <Tbody>
        {rows.map((r) => (
          <Tr key={r.path} data-testid="diff-row" data-path={r.path} data-kind={r.kind}>
            <Td className="font-mono text-xs">{r.path}</Td>
            <Td className="text-red-700">{r.oldValue}</Td>
            <Td className="text-green-700">{r.newValue}</Td>
          </Tr>
        ))}
      </Tbody>
    </Table>
  );
}
