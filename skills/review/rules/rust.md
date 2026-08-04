# Rust-specific review rules

Apply these in addition to the default rubric when reviewing `.rs` files.

## `.unwrap()` in non-test code — Correctness / high
`.unwrap()` panics on `None` or `Err(_)` with a generic message. In production
code this is almost always wrong: use `?` to propagate, `unwrap_or` / `unwrap_or_else`
for a default, or an explicit `match`/`if let` with meaningful error handling.
Exception: `.unwrap()` is acceptable in tests and in cases where a panic is
genuinely the right response (logic error, programmer mistake — document why).
```rust
// WRONG
let val = map.get("key").unwrap();

// CORRECT
let val = map.get("key").ok_or(MyError::MissingKey)?;
// or
let val = map.get("key").unwrap_or(&default_val);
```

## `.expect()` with non-descriptive messages — Correctness / medium
`.expect("failed")` is marginally better than `.unwrap()` but still produces
unhelpful panics. Messages should describe the invariant that was violated so
a developer reading a panic knows what assumption broke.
```rust
// POOR
config.get("timeout").expect("failed to get timeout");

// BETTER
config.get("timeout").expect("timeout key must be present in config (set in main.rs:42)");
```

## `Arc<Mutex<T>>` held across `.await` — Correctness / high
Holding a `MutexGuard` across an `.await` point can cause a deadlock: the
lock is held while the task is suspended, and if another task tries to acquire
it on the same thread, the executor may hang. Drop the guard before `.await`,
or use `tokio::sync::Mutex` instead of `std::sync::Mutex`.
```rust
// WRONG
let guard = shared.lock().unwrap();
some_async_fn().await;   // guard held across await — potential deadlock
drop(guard);             // too late; deadlock can occur before this

// CORRECT
{
    let guard = shared.lock().unwrap();
    prepare_data(&guard);
}  // guard dropped here
some_async_fn().await;
```

## Unnecessary `.clone()` — Performance / medium
`.clone()` on owned types (`String`, `Vec<T>`, etc.) performs a heap allocation.
Frequent or unnecessary clones in hot paths are a performance smell. Check
whether a borrow (`&str`, `&[T]`) or a reference suffices, or whether the
caller could transfer ownership instead.

## `unwrap()` on `Mutex::lock()` — Correctness / medium
`Mutex::lock()` returns `Err` only if the mutex is "poisoned" (another thread
panicked while holding the lock). Calling `.unwrap()` here propagates the panic
into the current thread. Prefer explicit handling or `lock().unwrap_or_else(|e| e.into_inner())`.

## Unused `Result` — Correctness / high
Rust warns on unused `Result`s, but `#[must_use]` can be suppressed. Flag any
function returning `Result` whose return value is discarded in new or modified code.

## Integer cast truncation — Correctness / medium
`as` casts in Rust silently truncate (`u64 as u32` wraps). Use `try_from` /
`try_into` to propagate an error instead of silently corrupting the value.
```rust
// WRONG
let small = big_value as u32;   // truncates if value > u32::MAX

// CORRECT
let small = u32::try_from(big_value)?;
```
