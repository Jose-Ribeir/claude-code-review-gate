const UNITS = ["B", "KiB", "MiB", "GiB"] as const;

export function formatSize(n: number): string {
  if (n < 0) {
    throw new RangeError("negative byte count");
  }
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${UNITS[unit]}`;
}
