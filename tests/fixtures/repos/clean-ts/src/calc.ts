import { mean } from "./stats.js";

export interface CartLine {
  readonly sku: string;
  readonly unitPrice: number;
  readonly quantity: number;
}

export interface CartTotal {
  readonly itemCount: number;
  readonly subtotal: number;
  readonly averageUnitPrice: number;
}

export function cartTotal(lines: readonly CartLine[]): CartTotal {
  const itemCount = lines.reduce((acc, line) => acc + line.quantity, 0);
  const subtotal = lines.reduce((acc, line) => acc + line.unitPrice * line.quantity, 0);
  const unitPrices = lines.map((line) => line.unitPrice);
  return {
    itemCount,
    subtotal: round2(subtotal),
    averageUnitPrice: unitPrices.length === 0 ? 0 : round2(mean(unitPrices)),
  };
}

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}
