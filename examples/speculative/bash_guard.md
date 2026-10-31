# bash_guard: friction log

Files: `examples/speculative/bash_guard.py` (144 lines, about 65 of them logic),
`tests/examples/speculative/test_bash_guard.py` (16 tests, all pass). Line numbers below are
`bash_guard.py` unless a test is named.

## Does

A `pre_tool` hook matches a `bash` call's `command` against `Options.patterns` (find /, cat of
binaries and logs, npm install, cargo build, git log without -n, grep -r without -l, journalctl,
docker logs; a command already piped to head, tail or wc is left alone). On a match it asks the
frontend `reject | allow | always | truncate`. `truncate` asks a second question for a grep pattern
and rewrites the pending call's `command` to `{ cmd } 2>&1 | grep -e PAT | tail -n 200`. A reject is
the call's `Err`, worded so the model can narrow the command. `always` lands in `Options.allow`
(a `Config` node), so it holds on the branch and after a reload. With nobody to answer, the
`unattended` option (default `reject`) decides; a connected client that never answers is given up
on after `wait` seconds. Note the base `bash` tool already keeps only the last 10 kB of output,
so the guard is about time and tokens spent, not about the final result size.

## Easy

- 98-102: a typed permission question is one `await node.ask(text, Literal[...])`; the schema,
  the check and the refusal of a wrong answer come free (`test_asking` territory, here
  `test_the_frontend_sees_a_busy_cursor_and_a_typed_question`).
- 118: the hook can rewrite a pending call, and the tool runs the edit (the test's `FakeTool`
  records the rewritten command). "No hook edit limits" made this one line.
- 121-125: reject is `return "reason"`; the model gets `Err(reason)`. Nothing to wire.
- 35-67: options are a dataclass with `Annotated` help and a `Literal`; `__post_init__` makes
  `/config bash_guard patterns=(` fail at configure time (`ConfigError`, nothing written).
- 78-84: `asyncio.timeout` around `ask` works as Q9 says; a timed-out question vanishes from the
  cursor state on its own.
- 143: `cursor.set_options(allow=...)` is branch-aware, persisted and shown by `/config`
  (`test_always_sticks_in_the_options_and_survives_a_reload`, with a brand-new `BashGuard()`
  instance, so only the tree remembers).
- A batch needs no code: three calls are three concurrent hooks, three questions (oldest first),
  results joined in call order; the first answer wins and a second client gets `LookupError`
  (`test_a_batch_asks_all_its_questions_at_once_and_clients_race_to_answer`).
- Abort needs no code: every open question is declined, the hooks finish, the step ends
  `Aborted` (`test_an_abort_declines_every_open_question`).

## Fought

### (a) Expressing the behaviour

1. **Record what really ran.** Intent: the tree (and so the user and a later reader) shows the
   rewritten command. API: `call.args_raw` is settable, but the `Assistant` node is immutable
   and holds the model's text; Q24 says core records no rewrite. Did: remember the rewrite in
   `self.ran` (76, 119) and prefix the result with `[bash_guard ran: ...]` in `pre_result`
   (127-136). Side effect: the model and the user both see the note as if the tool printed it.
   Severity: awkward. (A `Custom` node appended under the batch is on no walk, so it was not tried.)
2. **A list option cannot have a non-empty default.** Intent: built-in patterns visible and
   editable in `/config bash_guard`. API: the type language wants a `list` default, a dataclass
   refuses a mutable one, `default_factory` is refused by the options check, and a tuple is
   refused by the language (`its default ('a',) must be array, not object`, checked directly).
   Did: `list[str] | None = None` with `or BUILT_IN` (39-43, 63). The form then shows `null`
   (`test_the_patterns_are_options_but_the_built_ins_are_not_in_the_form`), so a person cannot see
   what they would replace. Severity: awkward, and it undercuts the headline "patterns editable
   from /config".
3. **`/config` cannot carry a comma.** `patterns=a{1,2}` parses to `["a{1", "2}"]`
   (`test_a_regex_with_a_comma_cannot_be_typed_to_config`). Regexes need commas. Did: nothing;
   a user edits the JSON config file instead. Severity: awkward.
4. **One hook sees every tool.** `call.tool != "bash"` (87) is the filter; there is no
   per-tool registration. Cosmetic. Likewise the hook re-parses `args_raw` (89-93) and must
   return `None` on bad JSON so the tool reports it. Cosmetic.

### (b) Showing state to the user

1. **A question is text and a schema, nothing else.** No tool call id, batch node, tool name or
   arguments, and an `enum` has no per-choice help ("truncate" means what?). A TUI cannot put the
   question beside the call it is about, only show the string (98-102). Severity: awkward.
2. **A tool hook cannot `tell`.** `tell` is on `Cursor` only, so "auto-rejected: nobody was there"
   is visible only inside the `Err` text. Severity: awkward.
3. **The rewrite is shown nowhere structured** (see (a)1): a frontend that draws the call from the
   `Assistant` node shows `find /`, then a result that is really `find / | tail -n 200`.
   Severity: awkward.

### (c) Getting input from the user

1. **`ask` returns `None` for four different things:** no client connected, the person declined
   (an empty line in `examples/chat.py`), an abort, and my own timeout. The hook cannot tell them
   apart and cannot ask "is anyone connected?". I treat all as "nobody answered" and the reject
   text says "nobody was there to approve it" even when a person declined (121). Severity: awkward.
2. **A two-part answer is two questions.** `truncate` then a grep pattern (110-115). The second
   question queues behind the other calls' first questions (oldest first), so in a batch the person
   answers "find /: truncate", "npm install: ?", and only then "grep -e what?"
   (`test_a_chat_line_client_asks_inline`). Because an empty line declines, "no pattern, just the
   tail" is spelled as a decline. Severity: awkward.
3. **No "reject, with this reason".** Same cause: one value per question. Cosmetic.
4. **Who to ask.** `to=None` is anyone; `node.client` is the typist, but a typist who closed the
   laptop declines at once even if a phone is attached. "The typist, else anyone" is not
   expressible in one call. I used anyone. Cosmetic.

### (d) Timing and concurrency

1. **"Always" cannot be written from a tool hook.** `set_options` is on `Cursor`; a `NodeHandle` has
   no cursor operations, which is right (a `Config` written under the batch node would not be on
   the walk of the `Results`). Did: `pre_tool` records the pattern per batch (104-107), `post_result`
   (a cursor hook, 138-144) writes it with `set_options` once the join is done. Three pieces and
   two dicts of extension memory for "remember this". Severity: awkward. (Adding an item to the cursor and writing it from a `pre_step`
   hook would also work; not tried.)
2. **Siblings ask again.** `always` on call a does not close b's and c's open questions about the
   same pattern (`test_always_in_a_batch_is_not_applied_to_the_questions_already_open`).
   Serializing the hooks with an `asyncio.Lock` fixes that and shows the frontend one question at a
   time, but it is **unsafe**: I tried it in a scratch run. After an abort declines the open question,
   the hooks waiting on the lock ask their own questions on the aborted step, and the cursor stays
   busy until a person answers them. A hook cannot learn its step was aborted. Severity: blocker for
   serializing; awkward without it.
3. **The Harness cannot wait on an extension's lock:** `settle()` sees units that are neither at a
   gate nor at a question and fails with "not quiet after 10000 passes" (same scratch run).
   Cosmetic, but it means such an extension is untestable without sleeping.
4. **Timeout uses the loop's clock, not the `Clock`.** `test_a_client_that_never_answers_is_given_up_on`
   has to sleep 0.2 s of real time. Cosmetic.
5. **Extension memory is per driver, not per session.** `self.always` and `self.ran` (75-76) are
   keyed by batch node and lost in a crash; harmless here (the person is asked again), but it is
   state outside the tree. Cosmetic.

## Frontend view

`examples/chat.py` prints, for the oldest open question only:

    ? bash would print a lot (matches /\bfind\s+\/(?:\s|$)/):
      find /
     [reject/allow/always/truncate]

and reads the answer from the next line. With three questions open it shows one and says nothing
of the other two; the follow-up grep question appears after them. A tool call's result, the
reject text and the `[bash_guard ran: ...]` note appear only as tool results (the line client prints
none of that).

A TUI or web frontend needs, per cursor, from `CursorView`: `busy` and `questions` (all cursors,
so "cursor 3 is waiting on you" works while it watches cursor 1), rendered as buttons from the
`enum`. It has to reconstruct by itself: which call a question belongs to (by parsing the text and
matching against the pending `Assistant` node's calls), the rewritten command (by parsing the
`[bash_guard ran:` prefix of the result), a stale question after losing a race (`LookupError` on
`answer`: close the dialog and wait for the next `CursorView`), and the configured `allow` list
(`Client.options`). Everything it needs is delivered; nothing tells it how the pieces relate.

## Wish

Smallest first.

1. **A question asked on a step that is already aborting is declined at once.** One rule, no new
   concept. Enables serializing the batch's questions with a three-line lock
   (`async with self.lock(node.id):` plus a re-check of `allow`), which fixes (d)2 and gives every
   frontend one question at a time. Deletes nothing today; removes the reason not to.
2. **`handle.connected(to=None) -> bool`** (or ask raising a distinct exception for "nobody
   there"). Deletes the guess at 121 and lets the unattended branch run before asking at all.
3. **Accept a tuple (or any `Sequence`) as a list option's default**, or allow `default_factory`
   in options. Deletes `| None`, `or BUILT_IN` and the hidden-default problem (lines 39-43, 50, 63)
   and makes `/config bash_guard` show the built-in patterns.
4. **`commandline.value` accepts a JSON array** when the text starts with `[`
   (`patterns='["a{1,2}"]'`). Fixes (a)3 with no new syntax for a name=value line.
5. **`OpenQuestion.call: tuple[int, str] | None`** (batch node and call id, set for a tool hook's
   ask). A TUI places the question under the call without parsing text.
6. **`tell` on `TreeHandle`** (it already has `client`): a tool hook tells the person the
   unattended outcome. Small, and drops notices for disconnected clients, which is right.
7. **The honest "this is fine".** Writing `always` through `post_result` (d1) is the right shape:
   a tool hook runs in parallel and cannot append to the cursor's walk. Document it as the idiom
   rather than add `NodeHandle.set_options`. Recording the rewrite (a1) is also an extension's job
   (the result note); a convention for results that carry notes would help frontends, but nothing
   in core.
