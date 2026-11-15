/** String helpers shared across the app. All pure, all total. */

export function capitalize(input: string): string {
  if (input.length === 0) {
    return input;
  }
  return input.charAt(0).toUpperCase() + input.slice(1);
}

export function truncate(input: string, maxLength: number): string {
  if (maxLength < 0) {
    throw new RangeError(`maxLength must be non-negative, got ${maxLength}`);
  }
  if (input.length <= maxLength) {
    return input;
  }
  const cut = Math.max(0, maxLength - 1);
  return `${input.slice(0, cut)}\u2026`;
}

export function slugify(input: string): string {
  return input
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}
