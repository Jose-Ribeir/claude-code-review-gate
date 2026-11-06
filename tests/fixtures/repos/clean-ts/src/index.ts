import { cartTotal, type CartLine } from "./calc.js";
import { clamp } from "./stats.js";
import { capitalize, slugify, truncate } from "./strings.js";

interface Report {
  readonly title: string;
  readonly slug: string;
  readonly summary: string;
}

export function buildCartReport(lines: readonly CartLine[]): Report {
  const total = cartTotal(lines);
  const title = `Cart: ${total.itemCount} item${total.itemCount === 1 ? "" : "s"}`;
  const summary =
    `subtotal ${total.subtotal.toFixed(2)}, ` +
    `avg unit price ${total.averageUnitPrice.toFixed(2)}`;
  return {
    title: capitalize(title),
    slug: slugify(title),
    summary: truncate(summary, 80),
  };
}

export function discountPercent(subtotal: number, floorPct = 0, ceilPct = 30): number {
  const tier = subtotal > 100 ? 10 : 5;
  return clamp(tier, floorPct, ceilPct);
}
