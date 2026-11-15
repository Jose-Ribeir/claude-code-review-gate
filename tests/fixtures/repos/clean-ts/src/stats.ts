export function sum(values: readonly number[]): number {
  return values.reduce((acc, v) => acc + v, 0);
}

export function mean(values: readonly number[]): number {
  if (values.length === 0) {
    throw new RangeError("mean of empty list");
  }
  return sum(values) / values.length;
}

export function clamp(value: number, min: number, max: number): number {
  if (min > max) {
    throw new RangeError(`min (${min}) must be <= max (${max})`);
  }
  return Math.min(Math.max(value, min), max);
}
