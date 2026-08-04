# Python-specific review rules

Apply these in addition to the default rubric when reviewing `.py` / `.pyi` files.

## Mutable default arguments — Correctness / high
A mutable object as a default parameter (`[]`, `{}`, `set()`) is shared across all
calls. New items accumulate silently across invocations.
```python
# WRONG
def append_item(item, lst=[]):
    lst.append(item)
    return lst

# CORRECT
def append_item(item, lst=None):
    if lst is None:
        lst = []
    lst.append(item)
    return lst
```

## Bare `except:` — Correctness / high
A bare `except:` clause catches `SystemExit`, `KeyboardInterrupt`, and
`GeneratorExit`, preventing clean shutdown. Use `except Exception:` at minimum;
prefer specific exception types.
```python
# WRONG
try:
    do_thing()
except:
    pass

# CORRECT
try:
    do_thing()
except ValueError as exc:
    handle(exc)
```

## `is` vs `==` for value comparison — Correctness / medium
`is` checks identity (same object in memory), not equality. Use `is` only for
`None`, `True`, `False`, and enum singletons. String/integer comparison with `is`
passes in tests (due to interning) and fails in production.
```python
# WRONG
if status is "active":   # may fail; strings not guaranteed interned
if count is 0:           # may fail for large integers

# CORRECT
if status == "active":
if count == 0:
if value is None:        # correct — None is always the same object
```

## Resource leaks — Correctness / high
Files, DB connections, sockets, and locks opened but not closed in a `finally`
block or `with` statement will leak the handle on exception.
```python
# WRONG
f = open("data.txt")
process(f.read())          # exception here leaks the file descriptor

# CORRECT
with open("data.txt") as f:
    process(f.read())
```

## f-strings in logging calls — Performance / low
An f-string in a `logging.*()` call is evaluated eagerly — even when the log
level would suppress the message. Use `%`-style or keyword args so the format
string is only rendered when the message will actually be emitted.
```python
# WRONG
logger.debug(f"Processing {len(records)} records: {records!r}")

# CORRECT
logger.debug("Processing %d records: %r", len(records), records)
```

## Late binding closures in loops — Correctness / medium
Lambda or nested-function captures a loop variable by reference; all closures see
the final value of the variable after the loop completes.
```python
# WRONG
actions = [lambda: print(i) for i in range(5)]
actions[0]()  # prints 4, not 0

# CORRECT
actions = [lambda i=i: print(i) for i in range(5)]
```

## Asyncio misuse — Correctness / high
- Calling `asyncio.run()` inside a running event loop raises `RuntimeError`.
- Calling a coroutine without `await` silently creates a coroutine object and
  never executes it (Python will warn, but may not raise).
- Mixing sync blocking calls (file I/O, `time.sleep`, `requests`) inside async
  functions blocks the event loop.
```python
# WRONG
async def handler():
    asyncio.run(other_coro())    # RuntimeError
    result = fetch_data()        # missing await — coroutine created but not run
    time.sleep(1)                # blocks event loop

# CORRECT
async def handler():
    await other_coro()
    result = await fetch_data()
    await asyncio.sleep(1)
```

## `__eq__` without `__hash__` — Correctness / medium
Defining `__eq__` makes the class unhashable by default (Python sets `__hash__`
to `None`). If instances need to go in a set or dict, also define `__hash__`.

## String concatenation in loops — Performance / medium
`result += chunk` in a loop creates a new string object on every iteration.
Use `"".join(parts)` or a `StringIO`.
