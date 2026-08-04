# TypeScript / JavaScript-specific review rules

Apply these in addition to the default rubric when reviewing `.ts`, `.tsx`,
`.js`, `.jsx`, `.mjs`, or `.cjs` files.

## Unsafe `any` typing — Maintainability / medium
`any` disables all type checking on a value and propagates silently. Prefer
`unknown` (requires narrowing before use) or a specific type. `as any` casts
are especially dangerous because they silence errors at the cast site while the
bug surfaces elsewhere.
```typescript
// WRONG
function process(data: any) { return data.value; }  // no type safety

// BETTER
function process(data: unknown) {
    if (typeof data === 'object' && data !== null && 'value' in data) {
        return (data as { value: string }).value;
    }
}
```

## Missing `await` on async calls — Correctness / high
Calling an `async` function without `await` returns a `Promise` instead of the
resolved value. The operation may execute, but errors are silently swallowed
unless the promise is properly handled. TypeScript's `@typescript-eslint/no-floating-promises`
catches this; in a review, look for assignments or conditions on async call results.
```typescript
// WRONG
const user = getUser(id);         // Promise, not User
if (user.isActive) { ... }        // always truthy (object)

// CORRECT
const user = await getUser(id);
```

## Swallowed promise rejections — Correctness / high
`.catch(() => {})`, an empty `.catch(e => console.log(e))` with no rethrow, or
`Promise.all` without a `.catch` can silently discard errors. Unhandled rejections
crash Node processes (Node ≥15) or produce invisible failures.
```typescript
// WRONG
fetchData().catch(() => {});   // error silently ignored

// CORRECT
fetchData().catch((err) => {
    logger.error('fetchData failed', err);
    throw err;                  // or handle meaningfully
});
```

## `null` / `undefined` confusion — Correctness / medium
In TypeScript strict mode, `null` and `undefined` are distinct. Returning
`undefined` where `null` is expected (or vice versa) can break callers that
do strict equality checks (`=== null`).

## Non-null assertion overuse — Correctness / medium
`value!` tells the compiler "I know this is not null/undefined." If that
assumption is wrong at runtime, the code throws with no useful error message.
Prefer an explicit guard or narrowing.
```typescript
// RISKY
const el = document.getElementById('app')!;    // crashes if element absent

// SAFER
const el = document.getElementById('app');
if (!el) throw new Error('Required element #app not found');
```

## `var` usage — Maintainability / low
`var` is function-scoped and hoisted; `let` / `const` are block-scoped and
predictable. New code should never use `var`.

## `==` vs `===` in JavaScript — Correctness / medium
`==` coerces types (`0 == ""` is `true`). Use `===` for all comparisons unless
explicit type coercion is intended. TypeScript catches many of these, but `.js`
files are not protected.

## Async callback in array methods — Correctness / high
`Array.prototype.forEach` ignores the return value, so `async` callbacks are
fire-and-forget — errors are lost and execution continues before the async work
completes. Use `for...of` with `await`, or `Promise.all(array.map(...))`.
```typescript
// WRONG
items.forEach(async (item) => {
    await process(item);    // errors silently lost; loop doesn't wait
});

// CORRECT
for (const item of items) {
    await process(item);
}
// or
await Promise.all(items.map(item => process(item)));
```

## Empty catch blocks — Correctness / medium
An empty `catch` block swallows the error completely. At minimum log it; ideally
rethrow or handle it.
