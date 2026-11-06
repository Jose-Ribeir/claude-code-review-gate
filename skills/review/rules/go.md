# Go-specific review rules

Apply these in addition to the default rubric when reviewing `.go` files.

## Ignored error returns — Correctness / high
Go functions signal failure through explicit `error` return values. Discarding
them with `_` or via implicit assignment means the caller silently continues
after a failure.
```go
// WRONG
os.Remove(path)         // error ignored
f, _ := os.Open(path)  // if Open fails, f is nil and next use panics

// CORRECT
if err := os.Remove(path); err != nil {
    return fmt.Errorf("remove %s: %w", path, err)
}
f, err := os.Open(path)
if err != nil {
    return err
}
```

## Goroutine leaks — Correctness / high
A goroutine that blocks forever on a channel or `select` with no cancellation
path leaks for the lifetime of the process. Always provide a way to stop:
a `context.Context`, a done channel, or a documented lifetime.
```go
// WRONG — goroutine runs until process exits
go func() {
    for v := range ch { process(v) }
}()

// CORRECT — goroutine respects cancellation
go func() {
    for {
        select {
        case v := <-ch:
            process(v)
        case <-ctx.Done():
            return
        }
    }
}()
```

## `sync.Mutex` copied by value — Correctness / high
Copying a `sync.Mutex` (or any sync type) resets its state, producing a usable
but logically independent lock — the copy and original protect different critical
sections while appearing to share the same resource.
```go
// WRONG
type Cache struct { mu sync.Mutex; data map[string]string }
c2 := c1  // mu is copied; c2.mu is a new, unlocked mutex

// CORRECT — always use pointer receivers for structs with sync fields
func (c *Cache) Set(k, v string) { c.mu.Lock(); ... }
```

## `context.Background()` in request scope — Correctness / medium
Using `context.Background()` inside a request handler discards any deadline,
cancellation, or value propagated by the caller. Thread the incoming `context.Context`
through instead.
```go
// WRONG
func HandleRequest(w http.ResponseWriter, r *http.Request) {
    result, err := db.QueryContext(context.Background(), query)

// CORRECT
    result, err := db.QueryContext(r.Context(), query)
```

## `defer` inside a loop — Correctness / medium
A deferred call runs when the enclosing **function** returns, not at the end of
the loop iteration. Defering a resource release inside a loop accumulates all
releases until the function exits, holding the resource for the entire loop.
```go
// WRONG — all rows.Close() calls run after the entire loop
for _, path := range paths {
    rows, _ := db.Query(path)
    defer rows.Close()
    // ... process rows ...
}

// CORRECT — close within the iteration
for _, path := range paths {
    func() {
        rows, _ := db.Query(path)
        defer rows.Close()
        // ... process rows ...
    }()
}
```

## Shadowed `err` variable — Correctness / medium
Using `:=` when an `err` variable is already in scope creates a new variable
that shadows the outer one. After the inner scope exits, the outer `err` still
holds its old value, so error checks after the block may be wrong.
```go
// WRONG
err := doFirst()
if err == nil {
    result, err := doSecond()   // new 'err', shadows outer
    _ = result
}
if err != nil {                 // checks outer 'err', not doSecond's
    return err
}
```

## Nil pointer dereference — Correctness / high
Dereferencing a nil interface, nil pointer, or nil map assignment panics at
runtime. Flag any dereference (`*p`, `p.Field`, `m[k]` assignment) that is not
preceded by a nil check when the value may legally be nil.

## Integer overflow in conversions — Correctness / medium
Converting a larger integer type to a smaller one silently truncates. Flag
`int64 → int32`, `int → int8`, etc. when the value may exceed the target range.
