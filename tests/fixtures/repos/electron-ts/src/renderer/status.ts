import { formatBytes } from "../main/util";

export function statusLine(used: number, total: number): string {
  const pct = total === 0 ? 0 : Math.round((used / total) * 100);
  return `disk ${formatBytes(used)} / ${formatBytes(total)} (${pct}%)`;
}
