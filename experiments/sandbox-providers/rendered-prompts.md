## cpython × script

### tool description

```text
Run a Python 3 script. Every other tool is an async function on `tools` (`await tools.read(path="a.txt")`); only what the script prints and its last expression's value are returned. Every call starts a fresh interpreter: variables, functions and imports from earlier calls are gone. See "Code mode" in the system prompt.
```

### system section

````markdown
## Code mode

The `codemode` tool runs Python 3 source that you write. Inside a script every other tool is an async function on the `tools` object, called with keyword arguments: `text = await tools.read(path="setup.py")`. A tool returns its text output as a `str`; a tool that fails raises `RuntimeError` carrying the tool's error message, which you can catch.

Use it when a task needs several tool calls, a loop, or filtering of large output. Run independent calls together with `await asyncio.gather(...)` (after `import asyncio`) and process results inside the script. Only what the script prints, and the value of its last expression, comes back to you: tool results the script does not print never enter the conversation, so print what you need, not raw dumps.

Every call starts a fresh interpreter: variables, functions and imports from earlier calls are gone. Carry anything you need forward by printing it, or recompute it.

This is full CPython with its standard library, run in a separate process with the same file access as the bash tool but no API keys in its environment. Prefer tools over direct file or network access, so the user sees what happened.

Limits per call: 30 s wall time (tool time included), 512 MiB memory, 1000 tool calls, 8000 characters of output (the rest is saved to a file whose path the result names).

`await search_tools("query")` returns `[{"name", "description"}]` for the tools that match, and `await describe_tool("name")` returns one tool's signature and full description. Use them for any tool not listed here.

Tools callable from scripts:
```python
class tools:
    async def read(*, path: str, offset: int | None = None, limit: int | None = None) -> str:
        """Read the contents of a file at the given path."""
    async def bash(*, command: str, timeout: int | None = None) -> str:
        """Execute a shell command via subprocess."""
    async def edit(*, path: str, old_string: str, new_string: str, replace_all: bool | None = None) -> str:
        """Replace old_string with new_string in a file."""
    async def write(*, path: str, content: str, encoding: str | None = None) -> str:
        """Write content to a file at the given path."""
    async def grep(*, pattern: str, path: str | None = None, files: list[str] | None = None, ignore_case: bool | None = None) -> str:
        """Search for a pattern in files using regex."""
    async def find(*, path: str | None = None, name: str | None = None, type: str | None = None, regex: str | None = None) -> str:
        """Find files matching criteria."""
    async def ls(*, path: str | None = None, all: bool | None = None, long: bool | None = None) -> str:
        """List directory contents."""
```
````

## cpython × turn

### tool description

```text
Run a Python 3 script. Every other tool is an async function on `tools` (`await tools.read(path="a.txt")`); only what the script prints and its last expression's value are returned. Calls made while answering one user message share an interpreter: variables, functions and imports persist from call to call until you reply, then the interpreter is reset. See "Code mode" in the system prompt.
```

### system section

````markdown
## Code mode

The `codemode` tool runs Python 3 source that you write. Inside a script every other tool is an async function on the `tools` object, called with keyword arguments: `text = await tools.read(path="setup.py")`. A tool returns its text output as a `str`; a tool that fails raises `RuntimeError` carrying the tool's error message, which you can catch.

Use it when a task needs several tool calls, a loop, or filtering of large output. Run independent calls together with `await asyncio.gather(...)` (after `import asyncio`) and process results inside the script. Only what the script prints, and the value of its last expression, comes back to you: tool results the script does not print never enter the conversation, so print what you need, not raw dumps.

Calls made while answering one user message share an interpreter: variables, functions and imports persist from call to call until you reply, then the interpreter is reset.

This is full CPython with its standard library, run in a separate process with the same file access as the bash tool but no API keys in its environment. Prefer tools over direct file or network access, so the user sees what happened.

Limits per call: 30 s wall time (tool time included), 512 MiB memory, 1000 tool calls, 8000 characters of output (the rest is saved to a file whose path the result names).

`await search_tools("query")` returns `[{"name", "description"}]` for the tools that match, and `await describe_tool("name")` returns one tool's signature and full description. Use them for any tool not listed here.

Tools callable from scripts:
```python
class tools:
    async def read(*, path: str, offset: int | None = None, limit: int | None = None) -> str:
        """Read the contents of a file at the given path."""
    async def bash(*, command: str, timeout: int | None = None) -> str:
        """Execute a shell command via subprocess."""
    async def edit(*, path: str, old_string: str, new_string: str, replace_all: bool | None = None) -> str:
        """Replace old_string with new_string in a file."""
    async def write(*, path: str, content: str, encoding: str | None = None) -> str:
        """Write content to a file at the given path."""
    async def grep(*, pattern: str, path: str | None = None, files: list[str] | None = None, ignore_case: bool | None = None) -> str:
        """Search for a pattern in files using regex."""
    async def find(*, path: str | None = None, name: str | None = None, type: str | None = None, regex: str | None = None) -> str:
        """Find files matching criteria."""
    async def ls(*, path: str | None = None, all: bool | None = None, long: bool | None = None) -> str:
        """List directory contents."""
```
````

## cpython × session

### tool description

```text
Run a Python 3 script. Every other tool is an async function on `tools` (`await tools.read(path="a.txt")`); only what the script prints and its last expression's value are returned. The interpreter persists across calls, like a REPL or a notebook kernel: variables, functions and imports defined in one call are there in the next, for the rest of this conversation. See "Code mode" in the system prompt.
```

### system section

````markdown
## Code mode

The `codemode` tool runs Python 3 source that you write. Inside a script every other tool is an async function on the `tools` object, called with keyword arguments: `text = await tools.read(path="setup.py")`. A tool returns its text output as a `str`; a tool that fails raises `RuntimeError` carrying the tool's error message, which you can catch.

Use it when a task needs several tool calls, a loop, or filtering of large output. Run independent calls together with `await asyncio.gather(...)` (after `import asyncio`) and process results inside the script. Only what the script prints, and the value of its last expression, comes back to you: tool results the script does not print never enter the conversation, so print what you need, not raw dumps.

The interpreter persists across calls, like a REPL or a notebook kernel: variables, functions and imports defined in one call are there in the next, for the rest of this conversation. It is reset after a timeout, a memory-limit trip or a crash, and the result says so when that happens; redefine what you need then.

This is full CPython with its standard library, run in a separate process with the same file access as the bash tool but no API keys in its environment. Prefer tools over direct file or network access, so the user sees what happened.

Limits per call: 30 s wall time (tool time included), 512 MiB memory, 1000 tool calls, 8000 characters of output (the rest is saved to a file whose path the result names).

`await search_tools("query")` returns `[{"name", "description"}]` for the tools that match, and `await describe_tool("name")` returns one tool's signature and full description. Use them for any tool not listed here.

Tools callable from scripts:
```python
class tools:
    async def read(*, path: str, offset: int | None = None, limit: int | None = None) -> str:
        """Read the contents of a file at the given path."""
    async def bash(*, command: str, timeout: int | None = None) -> str:
        """Execute a shell command via subprocess."""
    async def edit(*, path: str, old_string: str, new_string: str, replace_all: bool | None = None) -> str:
        """Replace old_string with new_string in a file."""
    async def write(*, path: str, content: str, encoding: str | None = None) -> str:
        """Write content to a file at the given path."""
    async def grep(*, pattern: str, path: str | None = None, files: list[str] | None = None, ignore_case: bool | None = None) -> str:
        """Search for a pattern in files using regex."""
    async def find(*, path: str | None = None, name: str | None = None, type: str | None = None, regex: str | None = None) -> str:
        """Find files matching criteria."""
    async def ls(*, path: str | None = None, all: bool | None = None, long: bool | None = None) -> str:
        """List directory contents."""
```
````

## monty × script

### tool description

```text
Run a Python (the Monty subset) script. Every other tool is an async function on `tools` (`await tools.read(path="a.txt")`); only what the script prints and its last expression's value are returned. Every call starts a fresh interpreter: variables, functions and imports from earlier calls are gone. See "Code mode" in the system prompt.
```

### system section

````markdown
## Code mode

The `codemode` tool runs Python (the Monty subset) source that you write. Inside a script every other tool is an async function on the `tools` object, called with keyword arguments: `text = await tools.read(path="setup.py")`. A tool returns its text output as a `str`; a tool that fails raises `RuntimeError` carrying the tool's error message, which you can catch.

Use it when a task needs several tool calls, a loop, or filtering of large output. Run independent calls together with `await asyncio.gather(...)` (after `import asyncio`) and process results inside the script. Only what the script prints, and the value of its last expression, comes back to you: tool results the script does not print never enter the conversation, so print what you need, not raw dumps.

Every call starts a fresh interpreter: variables, functions and imports from earlier calls are gone. Carry anything you need forward by printing it, or recompute it.

The interpreter is Monty, a sandboxed subset of Python. It has functions, closures, comprehensions, f-strings, try/except, dataclasses and async/await, and these modules: asyncio, json, re, math, datetime, collections, itertools, functools, typing, random, time, base64. It does NOT have class inheritance (so no custom exception classes), generators or `yield` (a generator expression builds a list, so `next(x for x in ...)` fails: index a list comprehension instead), `match`, `del`, or any third-party package, and it has no filesystem or network: use tools for those. `asyncio.gather` takes no keyword arguments, so there is no `return_exceptions`: wrap a call in try/except inside a helper coroutine to keep one failure from failing the gather.

Limits per call: 30 s wall time (tool time included), 512 MiB memory, 1000 tool calls, 8000 characters of output (the rest is saved to a file whose path the result names).

`await search_tools("query")` returns `[{"name", "description"}]` for the tools that match, and `await describe_tool("name")` returns one tool's signature and full description. Use them for any tool not listed here.

Tools callable from scripts:
```python
class tools:
    async def read(*, path: str, offset: int | None = None, limit: int | None = None) -> str:
        """Read the contents of a file at the given path."""
    async def bash(*, command: str, timeout: int | None = None) -> str:
        """Execute a shell command via subprocess."""
    async def edit(*, path: str, old_string: str, new_string: str, replace_all: bool | None = None) -> str:
        """Replace old_string with new_string in a file."""
    async def write(*, path: str, content: str, encoding: str | None = None) -> str:
        """Write content to a file at the given path."""
    async def grep(*, pattern: str, path: str | None = None, files: list[str] | None = None, ignore_case: bool | None = None) -> str:
        """Search for a pattern in files using regex."""
    async def find(*, path: str | None = None, name: str | None = None, type: str | None = None, regex: str | None = None) -> str:
        """Find files matching criteria."""
    async def ls(*, path: str | None = None, all: bool | None = None, long: bool | None = None) -> str:
        """List directory contents."""
```
````

## monty × turn

### tool description

```text
Run a Python (the Monty subset) script. Every other tool is an async function on `tools` (`await tools.read(path="a.txt")`); only what the script prints and its last expression's value are returned. Calls made while answering one user message share an interpreter: variables, functions and imports persist from call to call until you reply, then the interpreter is reset. See "Code mode" in the system prompt.
```

### system section

````markdown
## Code mode

The `codemode` tool runs Python (the Monty subset) source that you write. Inside a script every other tool is an async function on the `tools` object, called with keyword arguments: `text = await tools.read(path="setup.py")`. A tool returns its text output as a `str`; a tool that fails raises `RuntimeError` carrying the tool's error message, which you can catch.

Use it when a task needs several tool calls, a loop, or filtering of large output. Run independent calls together with `await asyncio.gather(...)` (after `import asyncio`) and process results inside the script. Only what the script prints, and the value of its last expression, comes back to you: tool results the script does not print never enter the conversation, so print what you need, not raw dumps.

Calls made while answering one user message share an interpreter: variables, functions and imports persist from call to call until you reply, then the interpreter is reset.

The interpreter is Monty, a sandboxed subset of Python. It has functions, closures, comprehensions, f-strings, try/except, dataclasses and async/await, and these modules: asyncio, json, re, math, datetime, collections, itertools, functools, typing, random, time, base64. It does NOT have class inheritance (so no custom exception classes), generators or `yield` (a generator expression builds a list, so `next(x for x in ...)` fails: index a list comprehension instead), `match`, `del`, or any third-party package, and it has no filesystem or network: use tools for those. `asyncio.gather` takes no keyword arguments, so there is no `return_exceptions`: wrap a call in try/except inside a helper coroutine to keep one failure from failing the gather.

Limits per call: 30 s wall time (tool time included), 512 MiB memory, 1000 tool calls, 8000 characters of output (the rest is saved to a file whose path the result names).

`await search_tools("query")` returns `[{"name", "description"}]` for the tools that match, and `await describe_tool("name")` returns one tool's signature and full description. Use them for any tool not listed here.

Tools callable from scripts:
```python
class tools:
    async def read(*, path: str, offset: int | None = None, limit: int | None = None) -> str:
        """Read the contents of a file at the given path."""
    async def bash(*, command: str, timeout: int | None = None) -> str:
        """Execute a shell command via subprocess."""
    async def edit(*, path: str, old_string: str, new_string: str, replace_all: bool | None = None) -> str:
        """Replace old_string with new_string in a file."""
    async def write(*, path: str, content: str, encoding: str | None = None) -> str:
        """Write content to a file at the given path."""
    async def grep(*, pattern: str, path: str | None = None, files: list[str] | None = None, ignore_case: bool | None = None) -> str:
        """Search for a pattern in files using regex."""
    async def find(*, path: str | None = None, name: str | None = None, type: str | None = None, regex: str | None = None) -> str:
        """Find files matching criteria."""
    async def ls(*, path: str | None = None, all: bool | None = None, long: bool | None = None) -> str:
        """List directory contents."""
```
````

## monty × session

### tool description

```text
Run a Python (the Monty subset) script. Every other tool is an async function on `tools` (`await tools.read(path="a.txt")`); only what the script prints and its last expression's value are returned. The interpreter persists across calls, like a REPL or a notebook kernel: variables, functions and imports defined in one call are there in the next, for the rest of this conversation. See "Code mode" in the system prompt.
```

### system section

````markdown
## Code mode

The `codemode` tool runs Python (the Monty subset) source that you write. Inside a script every other tool is an async function on the `tools` object, called with keyword arguments: `text = await tools.read(path="setup.py")`. A tool returns its text output as a `str`; a tool that fails raises `RuntimeError` carrying the tool's error message, which you can catch.

Use it when a task needs several tool calls, a loop, or filtering of large output. Run independent calls together with `await asyncio.gather(...)` (after `import asyncio`) and process results inside the script. Only what the script prints, and the value of its last expression, comes back to you: tool results the script does not print never enter the conversation, so print what you need, not raw dumps.

The interpreter persists across calls, like a REPL or a notebook kernel: variables, functions and imports defined in one call are there in the next, for the rest of this conversation. It is reset after a timeout, a memory-limit trip or a crash, and the result says so when that happens; redefine what you need then.

The interpreter is Monty, a sandboxed subset of Python. It has functions, closures, comprehensions, f-strings, try/except, dataclasses and async/await, and these modules: asyncio, json, re, math, datetime, collections, itertools, functools, typing, random, time, base64. It does NOT have class inheritance (so no custom exception classes), generators or `yield` (a generator expression builds a list, so `next(x for x in ...)` fails: index a list comprehension instead), `match`, `del`, or any third-party package, and it has no filesystem or network: use tools for those. `asyncio.gather` takes no keyword arguments, so there is no `return_exceptions`: wrap a call in try/except inside a helper coroutine to keep one failure from failing the gather.

Limits per call: 30 s wall time (tool time included), 512 MiB memory, 1000 tool calls, 8000 characters of output (the rest is saved to a file whose path the result names).

`await search_tools("query")` returns `[{"name", "description"}]` for the tools that match, and `await describe_tool("name")` returns one tool's signature and full description. Use them for any tool not listed here.

Tools callable from scripts:
```python
class tools:
    async def read(*, path: str, offset: int | None = None, limit: int | None = None) -> str:
        """Read the contents of a file at the given path."""
    async def bash(*, command: str, timeout: int | None = None) -> str:
        """Execute a shell command via subprocess."""
    async def edit(*, path: str, old_string: str, new_string: str, replace_all: bool | None = None) -> str:
        """Replace old_string with new_string in a file."""
    async def write(*, path: str, content: str, encoding: str | None = None) -> str:
        """Write content to a file at the given path."""
    async def grep(*, pattern: str, path: str | None = None, files: list[str] | None = None, ignore_case: bool | None = None) -> str:
        """Search for a pattern in files using regex."""
    async def find(*, path: str | None = None, name: str | None = None, type: str | None = None, regex: str | None = None) -> str:
        """Find files matching criteria."""
    async def ls(*, path: str | None = None, all: bool | None = None, long: bool | None = None) -> str:
        """List directory contents."""
```
````

## typescript × script

### tool description

```text
Run a TypeScript script. Every other tool is an async method on `tools` (`await tools.read({ path: "a.txt" })`); only `console.log` output and the returned value come back. Every call runs in a fresh isolate: nothing from an earlier call survives.
```

### system section

````markdown
## Code mode

The `codemode` tool runs TypeScript that you write, as the body of an async function. Every other tool is an async method on the ambient `tools` object, taking one object of named arguments: `const text = await tools.read({ path: "setup.py" })`. A tool resolves to its text output; a tool that fails rejects with an `Error` carrying the tool's message.

Run independent calls together with `await Promise.all([...])` and process results inside the script. Only what you `console.log`, and the value you `return`, comes back to you.

Every call runs in a fresh isolate: nothing from an earlier call survives.

There is no `require`, no `fetch` and no filesystem: use tools.

Limits per call: 30 s wall time (tool time included), 512 MiB memory, 1000 tool calls, 8000 characters of output (the rest is saved to a file whose path the result names).

`await searchTools("query")` and `await describeTool("name")` find tools not listed here.

Tools callable from scripts:
```ts
declare const tools: {
  /** Read the contents of a file at the given path. */
  read(args: { path: string; offset?: number; limit?: number }): Promise<string>;
  /** Execute a shell command via subprocess. */
  bash(args: { command: string; timeout?: number }): Promise<string>;
  /** Replace old_string with new_string in a file. */
  edit(args: { path: string; old_string: string; new_string: string; replace_all?: boolean }): Promise<string>;
  /** Write content to a file at the given path. */
  write(args: { path: string; content: string; encoding?: string }): Promise<string>;
  /** Search for a pattern in files using regex. */
  grep(args: { pattern: string; path?: string; files?: Array<string>; ignore_case?: boolean }): Promise<string>;
  /** Find files matching criteria. */
  find(args: { path?: string; name?: string; type?: string; regex?: string }): Promise<string>;
  /** List directory contents. */
  ls(args: { path?: string; all?: boolean; long?: boolean }): Promise<string>;
};
```
````

## typescript × turn

### tool description

```text
Run a TypeScript script. Every other tool is an async method on `tools` (`await tools.read({ path: "a.txt" })`); only `console.log` output and the returned value come back. Calls made while answering one user message share an isolate; globals persist until you reply.
```

### system section

````markdown
## Code mode

The `codemode` tool runs TypeScript that you write, as the body of an async function. Every other tool is an async method on the ambient `tools` object, taking one object of named arguments: `const text = await tools.read({ path: "setup.py" })`. A tool resolves to its text output; a tool that fails rejects with an `Error` carrying the tool's message.

Run independent calls together with `await Promise.all([...])` and process results inside the script. Only what you `console.log`, and the value you `return`, comes back to you.

Calls made while answering one user message share an isolate; globals persist until you reply.

There is no `require`, no `fetch` and no filesystem: use tools.

Limits per call: 30 s wall time (tool time included), 512 MiB memory, 1000 tool calls, 8000 characters of output (the rest is saved to a file whose path the result names).

`await searchTools("query")` and `await describeTool("name")` find tools not listed here.

Tools callable from scripts:
```ts
declare const tools: {
  /** Read the contents of a file at the given path. */
  read(args: { path: string; offset?: number; limit?: number }): Promise<string>;
  /** Execute a shell command via subprocess. */
  bash(args: { command: string; timeout?: number }): Promise<string>;
  /** Replace old_string with new_string in a file. */
  edit(args: { path: string; old_string: string; new_string: string; replace_all?: boolean }): Promise<string>;
  /** Write content to a file at the given path. */
  write(args: { path: string; content: string; encoding?: string }): Promise<string>;
  /** Search for a pattern in files using regex. */
  grep(args: { pattern: string; path?: string; files?: Array<string>; ignore_case?: boolean }): Promise<string>;
  /** Find files matching criteria. */
  find(args: { path?: string; name?: string; type?: string; regex?: string }): Promise<string>;
  /** List directory contents. */
  ls(args: { path?: string; all?: boolean; long?: boolean }): Promise<string>;
};
```
````

## typescript × session

### tool description

```text
Run a TypeScript script. Every other tool is an async method on `tools` (`await tools.read({ path: "a.txt" })`); only `console.log` output and the returned value come back. The isolate persists across calls, like a REPL: top-level `globalThis` assignments are there in the next call.
```

### system section

````markdown
## Code mode

The `codemode` tool runs TypeScript that you write, as the body of an async function. Every other tool is an async method on the ambient `tools` object, taking one object of named arguments: `const text = await tools.read({ path: "setup.py" })`. A tool resolves to its text output; a tool that fails rejects with an `Error` carrying the tool's message.

Run independent calls together with `await Promise.all([...])` and process results inside the script. Only what you `console.log`, and the value you `return`, comes back to you.

The isolate persists across calls, like a REPL: top-level `globalThis` assignments are there in the next call. `const`/`let` are per call.

There is no `require`, no `fetch` and no filesystem: use tools.

Limits per call: 30 s wall time (tool time included), 512 MiB memory, 1000 tool calls, 8000 characters of output (the rest is saved to a file whose path the result names).

`await searchTools("query")` and `await describeTool("name")` find tools not listed here.

Tools callable from scripts:
```ts
declare const tools: {
  /** Read the contents of a file at the given path. */
  read(args: { path: string; offset?: number; limit?: number }): Promise<string>;
  /** Execute a shell command via subprocess. */
  bash(args: { command: string; timeout?: number }): Promise<string>;
  /** Replace old_string with new_string in a file. */
  edit(args: { path: string; old_string: string; new_string: string; replace_all?: boolean }): Promise<string>;
  /** Write content to a file at the given path. */
  write(args: { path: string; content: string; encoding?: string }): Promise<string>;
  /** Search for a pattern in files using regex. */
  grep(args: { pattern: string; path?: string; files?: Array<string>; ignore_case?: boolean }): Promise<string>;
  /** Find files matching criteria. */
  find(args: { path?: string; name?: string; type?: string; regex?: string }): Promise<string>;
  /** List directory contents. */
  ls(args: { path?: string; all?: boolean; long?: boolean }): Promise<string>;
};
```
````

