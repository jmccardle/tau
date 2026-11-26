# What τ does for you

Nine things happen in a session without any extension of yours: a failed request is tried
again, `@file` in a message attaches the file, files you write become commands (prompt templates
and skills), what the session has used and spent is shown, a long conversation can be shortened
(`/compact`, `/prune`), the model can hand a job to a sub-agent, `/undo` takes back the last
change, `/clear`, `/branch`, `/tree` and `/export` work on the session, and the model can read a
web page (`fetch`). Five more wait for you to ask: rules that ask before a tool runs (permissions),
`/plan` (plan mode, which does nothing until you type it), and the five that are off until you turn
them on: the bash guard, the router, tools from MCP servers, web search and shell commands at hook
points (hooks). Each can be switched
off, and each is a few lines to use. The examples run with the mock model, which needs no server
and no key.

```json file=$TAU_HOME/config.json
{
  "default_model": "mock:echo",
  "models": {
    "mock": { "provider": "mock", "models": { "echo": { "model": "echo" } } }
  }
}
```

## A failed request is tried again

A hosted model answers "overloaded" now and then. τ waits and asks again, up to three more times,
before it gives up: on HTTP 408, 429, 502, 503, 504 and 529, on a 500 that carries a `Retry-After`,
and on a connection that failed before any answer came back. It waits as long as the server's
`Retry-After` says, else 1, 2 and 4 seconds, and a `Retry-After` over 60 seconds fails at once with
the wait in the message. It never retries a plain 500 (a local server such as llama.cpp answers one
for a chat template error, which asking again cannot fix), and it never retries once a word of the
reply has arrived, because that word is already on your screen.

While it waits, the terminal UI's status bar says why and for how long:

```text
model: openai:gpt  ·  working…
HTTP 529: overloaded_error: Overloaded: retry 1 of 3 in 1 s
```

The wait is part of the step, so Esc cancels it. The line goes when the next try starts to answer.
`tau -p` has no status bar: it writes the same line to standard error for each wait, prefixed
`tau: ` (`tau: HTTP 429: slow down: retry 1 of 3 in 2 s`), with or without `--verbose`. When the
third retry fails too, the error says `(still failing after 3 retries)`.
`tau`, `tau -p` and `tau serve` all retry; a program that builds its own `Driver` wraps its
providers in `RetryProvider` ([`examples/retry.py`](../../examples/retry.py)). There is nothing
to tune. If a proxy in front of the model already retries, or you are debugging a provider, turn
it off like any built-in:

```bash
tau ext disable retry
tau ext list | grep -A1 '^retry'
tau ext enable retry
```

```text expect
set load.retry = false in <*>
retry         off        built-in (provider wrapper)
              load.retry = false in <*>
set load.retry = true in <*>
```

That writes `{"load": {"retry": false}}` into your config file. `--no-extensions` does not turn it
off; it skips only installed packages and drop-ins. To turn it off for one run, use a session
setting: `tau -p --set load.retry=false "..."`, or `TAU_SETTINGS='{"load": {"retry": false}}'` in
the environment (see [Session settings](configuration.md#session-settings)).

## `@file` in a message

Name a file with `@` and the model gets its text after your message:

```bash
printf 'fn main() {}\n' > main.rs
tau -p -m mock:echo -n attach "explain @main.rs"
```

```text expect
mock: explain @main.rs

<attachment path="main.rs">
fn main() {}

</attachment>
```

(The mock model echoes what it was sent, so you see the whole message.) A relative path is relative
to the session's working directory, and `~` is the home directory of whoever runs the session; over
`tau serve` that is the server's, not your terminal's. A token is `@` and the non-space characters
after it, at the start of the message or after a space, and trailing punctuation does not count:
`see @notes.txt.` attaches `notes.txt`. A path with spaces goes in quotes, `@"my notes.txt"`, or has each space escaped as in a shell,
`@my\ notes.txt` (`\\` is a backslash); Tab writes the quoted form. A word
that names no regular file stays as you typed it (`@alice`, a missing file, a directory), and a
file named twice is attached once. When a word looks like a path (it has a `/`, an extension or a
`:10` after it) but attaches nothing, τ tells you in a notice (a toast in the terminal UI, a line
on standard error under `-p`), so a typo does not go unnoticed; `john@host`, `@alice` and anything
inside backticks stay silent.

A file over 10 kB, a file that is not UTF-8 and a file that cannot be read are not pasted. The
model is given a reference instead, so it can `read` the part it needs:

```text
<reference path="/work/big.log" size="2.1 MB" reason="over the 10 kB limit"/>
```

An image file (a PNG, JPEG, GIF or WebP, judged by its bytes) is attached as an image, which a
model that can see is shown after your text. The text gains one line naming it,
`[image @shot.png: image/png 640x480]`. The 10 kB limit is for text; an image may be up to 20 MiB.
A model whose config lacks `images: true` does not take it: the message is not sent, and a notice
says why.

The limit is an option, in bytes. `--set attachments.limit=100000` changes it for one run,
`/config attachments limit=100000 --save` for good. `tau ext disable attachments` turns `@file` off.
The terminal UI completes the path when you press Tab after an `@` word. It offers only what would
attach (directories, and regular files; no link to nothing, no FIFO), and puts a path with spaces
in quotes. A `</attachment>` inside a file is written `<\/attachment>`, so a file cannot close its
own block and start another.

Only what you type is expanded. A message that an extension or a tool wrote (the task the model
gives a sub-agent, the text of a prompt template) is sent as written: if the model could name a
file with `@`, a rule that denies it the file would mean nothing. The arguments you type after a
template's command are yours: `/review @main.rs` attaches `main.rs` (after the template's whole
text), while an `@path` written in the template file itself stays text.

## Prompt templates and skills

A **prompt template** is a Markdown file whose name is a command. Put `review.md` in
`$TAU_HOME/prompts/` and `/review` exists:

```bash
mkdir -p "$TAU_HOME/prompts"
cat > "$TAU_HOME/prompts/review.md" <<'EOF'
---
description: Review a file for bugs
argument-hint: <file> [focus]
---
Review $1 for bugs. Focus on: $2
EOF
tau ext list --json | grep '"key": "skills"' | grep -o '"commands": \[[^]]*\]'
```

```text expect
"commands": ["skills:review"]
```

Typing `/review main.rs "error handling"` sends

```text
Review main.rs for bugs. Focus on: error handling
```

as your message, and the model answers it. How the arguments are read:

- `$ARGUMENTS` is everything after the command, as typed.
- `$1`, `$2` and on are its words. Words are split on whitespace, and double quotes group words
  (`"error handling"` is one word). A single quote is just a character, so `don't` is a word.
  `$10` and up are numbers of more than one digit: `$10` is the tenth word, not `$1` and `0`.
- A word the arguments do not have is empty text, so `/review main.rs` works with the hint
  `<file> [focus]` above: `$2` is nothing.
- `${2}` is `$2` where a digit follows (`${1}0` is the first word and a `0`). `$$` writes a literal
  `$` (`costs $$5`), the way `make` does; a backslash is a character in the arguments, so `\$` is
  not an escape. A `$` followed by anything else is left as it is.
- `@file` words in the arguments attach their files after the whole text (once each), as they do
  in a message; the template's own `@` words do not.
- A template with no placeholder gets the arguments after a blank line, which makes `Be brief.` a
  template you can follow with any question.
- A `"` that never closes, or a template that would send nothing (no text and no arguments), is
  refused like an empty message: one line, `/review: <reason> (usage: /review <file> [focus])`,
  and nothing is sent.

The command's help is the `description`; without one it is the first line of the file.
`argument-hint` is what the arguments are: the terminal UI shows it beside the name in the `/` list
and after `/review `, and in the form that asks for them.

Under `tau -p`, standard input is not an argument. `git diff | tau -p "/review main.rs"` sends the
template's text, a blank line, then the diff, exactly as it came. `/review` is run with the line
you typed, and a command that has no such use for input (`/model`) refuses it.

A **skill** is a directory with a `SKILL.md`, in `$TAU_HOME/skills/NAME/`, for instructions that
come with files of their own, such as scripts. It needs a `description`:

```bash
mkdir -p "$TAU_HOME/skills/pdf"
cat > "$TAU_HOME/skills/pdf/SKILL.md" <<'EOF'
---
description: Extract text and tables from PDF files
---
Use `pdftotext -layout FILE -` for text. Relative paths here start at this file's directory.
EOF
tau ext list --json | grep '"key": "skills"' | grep -o '"commands": \[[^]]*\]'
```

```text expect
"commands": ["skills:pdf", "skills:review"]
```

`/pdf report.pdf` sends the skill's text, wrapped in `<skill name="pdf" path="…/SKILL.md">`, and
a line telling the model that a relative path in the skill starts from the skill's directory,
then `report.pdf`. Nothing in the skill's own text is attached: an `@path` there is the model's to
`read` (attachments expand only what you type). A skill also reaches the model by itself: at the
first step of a new session,
when the model has a `read` tool, τ adds one system message that lists each skill's name,
description and path, and the model `read`s the file when a task matches. A skill that should only
run when you type it says `disable-model-invocation: true` in its frontmatter and is left out of
that list. The list is written once, with the system prompt, so the model's cache stays warm; a
skill you add later reaches new sessions only. The model reads a listed skill's file from disk when
it needs it, as it would any file, so what it reads is what the file says then: trust pins the
bytes `/name` sends, not the bytes the model reads later. `--set skills.advertise=false` leaves the list out.

A generated command gives way: when any other command has the name (`/model`, `/name`, an
extension's), the bare `/NAME` is that command and the file is `/skills:NAME`, which always means
the file. A file named `config` is skipped with a warning: `/config` is the built-in command.

`tau -p "/review main.rs"` prints the model's reply, after the command's own result if it has one,
as `tau -p "review main.rs"` would.

### The files

| where | what |
|---|---|
| `$TAU_HOME/prompts/NAME.md` | a template |
| `$TAU_HOME/skills/NAME/SKILL.md` | a skill |
| `.tau/prompts/NAME.md`, `.tau/skills/NAME/SKILL.md` in a trusted project | the same, for the project; for one name the project's wins |
| `~/.agents/skills/NAME/SKILL.md`, `~/.claude/skills/NAME/SKILL.md` | a skill, so a skill written for Claude Code or another agent loads as it is |
| `.agents/skills/NAME/SKILL.md`, `.claude/skills/NAME/SKILL.md` in a trusted project | the same, for the project, pinned by trust like `.tau/skills`. For one name `.tau/skills` wins over `.agents/skills`, which wins over `.claude/skills` |

For one name, **a project's file hides the user's**: if `.tau/prompts/review.md` and
`$TAU_HOME/prompts/review.md` both exist in a trusted project, `/review` is the project's, and
nothing says the user's was hidden. A project's skill hides a user's template of that name too.

A name is ASCII letters, digits, `_` and `-`. A file τ cannot use is **skipped with a warning**
that names the file and why, and τ starts without it: a bad name, a file that is not UTF-8 or
cannot be read, a skill without a description, frontmatter that does not parse, or a file in the
wrong place: `skills/pdf/skill.md` (the file is `SKILL.md`, in capitals), a flat `skills/pdf.md`
(a skill is a directory), `skills/group/pdf/SKILL.md` (skills are not read from subdirectories),
`prompts/sub/x.md` (templates are not read from subdirectories) and `prompts/x.MD` (`.md` in lower
case). This holds for every directory, a trusted project's included, and the others still load. The
warnings are printed on standard error at the start (and by `tau config check`), and `tau trust`
lists them without refusing the project. Only a template and a skill with one name in one directory
stop the start, since which one you meant is not for τ to guess.

A skill, a template, or a directory between (`.claude/skills`, say) may be a **symlink**, so
`.claude/skills -> .agents/skills` or a skill directory that links to a shared collection works,
inside the project or out of it. What τ reads through the link is what `tau trust` pins, so a
changed target asks for `tau trust` again, and the report names the path each linked file resolves
to. A link that leads nowhere, to something that is not a regular file, or to a file over 1 MiB
is refused, as any such file is.
`tau ext disable skills` starts without any of them. A byte order mark at the start of a file is ignored.

A file may begin with frontmatter, YAML `key: value` lines between two `---` lines. Read are
`description`, `argument-hint`, `disable-model-invocation` (`true` or `false`) and `name` (which,
if given, must be the file's name). Values are read as YAML reads them: plain text that goes on
over indented lines, `'single'` and `"double"` quoted text, and `|` (lines kept) and `>` (lines
folded) block scalars, as in `description: >-` followed by indented lines. Other keys, the lists
and mappings under them, and comments are ignored, so a Claude Code skill loads as it is. One of
the four keys whose value is empty, a list or a mapping is refused.

A project's files are text the model will be told to follow, so they are pinned like the rest of
`.tau/`: `tau trust` lists them and shows the text of each that is new or changed (the first 2000
characters of a file, with a line saying how much was cut), a project is asked about again when one
is added, removed or changed, and τ reads the bytes that were approved. Because they are pinned, a τ that predates this
asks once more about every project it trusted before ([Configuration](configuration.md#projects-and-trust)).

Templates and skills are read when τ starts, and again whenever the files have changed since,
before a template or skill command runs and before `/name` is called unknown. A running `tau` or
`tau serve` therefore sees a file you edit, add or delete: no restart, no timer, only a look at
the directories when you use a command. A project's files stay pinned: one changed since you
trusted it is not used, and its command fails with the report `tau trust` gives, so you run
`tau trust` again. A file edited into an error makes its own command fail with the reason, and
the others go on working. The skills list the model sees is the exception: it is written once, at a
session's first step, so a skill you add or edit reaches the model's list in new sessions only,
while `/name` already runs the new file.

## What it used and what it cost

After each reply the status bar shows what the reply took, and the session's running cost:

```text
usage: 12.3k in · 96% cached · 812 out · $0.017 · ctx 7% · $0.017 session
```

`12.3k in` is the prompt, `96% cached` the part of it the server read from its prompt cache (left
out when the server does not say), `812 out` the reply. `$0.017` and the session total appear for
a model that has a `usage.price`, and `ctx 7%` for one that has a `context_window`: the share of the
window the prompt and the reply fill. τ cannot know either number, so you write them in the
model's settings in your config. A model with no price shows no cost, never `$0`; a session total
that had to leave out replies of such a model ends in `+`.

```json
{
  "models": {
    "sonnet": {
      "provider": "anthropic",
      "model": "claude-sonnet-4-5",
      "context_window": 200000,
      "usage": {
        "price": { "input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75 }
      }
    }
  }
}
```

The price is in `usage`'s own section of the model, `models.<name>.usage`: a model's settings
hold one section per extension that reads one, named for it. (The old `models.<name>.price` is a
config error that says where it went.) A section that `usage` does not accept, such as a key it
does not have, is an error in `tau config check` and stops a start; a section named for an
extension that is not loaded is a warning and is ignored.

A price is US dollars per million tokens, and all four fields are required: a model that does not
charge for writing the cache repeats its `input` price as `cache_write`. A price with a field
missing, below 0, or not a number is a config error naming the model. Groups hand a price down like
any setting, and a project can change one field (`"usage": {"price": {"output": 20}}`). A fifth field,
`cache_write_1h`, is optional: what a cache write kept for an hour costs, when the server reports
those apart (Anthropic does). Without it they are priced as `cache_write`.

When a server says how large its window really is (llama.cpp does) and your `context_window`
says otherwise, `/usage` tells you (`local:qwen: the server reports a window of 32768 tokens; the
config says 40960 (context_window)`). tau goes on using yours.

A reply that was aborted before the server reported usage says so, and `/usage` counts it apart
("1 reply was aborted and reported no usage"). A reply whose usage could not be read says why, on
this line and in `/usage`. Counts are written `812`, `12.3k`, `1.2M` and `1.2G`, here and on the
badge of each reply in the TUI.

`/usage` prints the whole session, one row per model, with the replies of summaries and
sub-agents included, and ends with how counts are made before a request is sent:

```text
model             samples   input  cached  written  output   cost
local:qwen              3   36.9k       -        -    2.4k      -
anthropic:sonnet       12  210.0k     91%     4.1k    9.8k  $1.27
total                  15  246.9k     91%     4.1k   12.2k  $1.27
no price, so no cost: local:qwen
counts: reported only, with byte ceilings for what is not sent yet
```

`/usage json` answers the same figures as one JSON object, for a program: `models` (a row per
model), `total` and `cost_complete`. Costs are exact decimal strings in dollars, and `null` for a
model with no price. [Conventions](conventions.md) says what each field means, and how a client
that wants no round trip can sum `Assistant.meta.usage` itself.

Cost is worked out when you look, from the prices in the config the session was started with:
correct a price and start the session again (`tau -c`, `tau -r`), and the whole session is
priced anew. A reply is priced by the model it was sampled with, so a session that changed models
half way adds up right. The sums are exact, not floating point. A session made with `--fork`
starts with a copy of the source's walk, and its `/usage` counts the replies it copied too.

When a reply reads nothing from the prompt cache (the server said `cached: 0`) soon after a reply
of the same model that should have written it, τ tells you once per model, with the size of the
prompt that went at full price. It is about money, so only a model with a `usage.price` gets it.
`--set usage.cache_notice=false` silences it, and
`tau ext disable usage` removes all of this.

A reply an extension wrote in place of a sample (a `pre_sample` draft) was never billed, and is
not counted.

### A spending limit

`--set usage.max_usd=5` (or `max_usd = 5` under `[extensions.usage]`, or `/config usage`) stops the
session at $5 of spend, the total `/usage` shows for the whole tree. Unset, the default, there is
no limit. Once the spend reaches it, new input is refused and a running tool loop halts before its
next request, both with `budget: $5.00 of $5.00`.

Like every option, `max_usd` is read at the cursor's position: `/config usage` sets it for the
branch you are on, and a branch you start elsewhere in the tree does not have it (it still counts
all of the session's spend, though). To cover every branch, set it in your config
(`tau config set usage.max_usd=5`, or `--set usage.max_usd=5` for one run).

To go on after a halt, raise the limit (`/config usage`, or `tau config set`) and turn auto-step
back on: a halt turns it off, and the tool results already in the tree go to the model on the
next step. New input is accepted again as soon as the limit is above the spend.

The hooks that refuse run before the ones you write, so a hook of yours that would answer a sample
for nothing is halted too. A reply a `pre_reply` hook re-attributes (changes its `by` from the
provider) is not counted as spend.

A reply that reported no usage (a provider that sends none, or a reply you cut off with Esc) is
not free. τ estimates it: the request it answered from the counts of the walk (what `/usage`
and the compaction limits use: the server's own report for the prefix, the `tokens` extension's
count or the node's byte ceiling for what is new) and its output from the text it did produce,
priced at its model. The estimate leans high, so a limit errs towards having spent. When a later
reply reports its usage, τ corrects the estimate: the input that reply holds beyond the earlier
reply's (its input and output) is what the messages, results and unreported replies between them
cost, and it is split among them in proportion to their estimates. Nothing is rewritten; the
correction is worked out whenever the spend is read. `/usage` shows the estimate on its own line
(`~$0.04 estimated for 2 replies that reported no usage (counted toward max_usd, not in the totals above)`) and keeps it out of its totals.

A limit needs prices. If a model the session uses has no `usage.price`, τ cannot estimate what it
spent, and refuses rather than count it as free: `budget: no price for model local:qwen, set its
price or unset usage.max_usd`.

`tau -p --verbose` prints the same line to stderr (`tau: usage: …`) whenever it changes, as it does
for any extension that shows a line.

## How a request is counted before it is sent

What a server has billed is known exactly. What it has not seen yet (the message you just typed, a
tool result that just came back) has to be counted some other way, and the `tokens` built-in,
which is on by default, learns to. Each time two replies in a row show what the nodes between
them cost, it adds that to a small record for the model under `$TAU_HOME/calibration/`: sums for
a fit, never your text. After ten such spans it counts new nodes itself, as an estimate with a
bound, written `~1.2k`. Before that, and for a model it has never seen, a new node is counted by
its bytes, written `≤4.8k`, which is safe and about four times too many.

`/tokens` says where each model stands:

```text
anthropic:sonnet: heuristic ±6% for new nodes, from 41 replies (no tokenizer: pip install 'ffwf-tau[tokens]')
local:qwen: learning from 4 replies (3 of 10 proved spans since it last changed); byte ceilings until then (no tokenizer: pip install 'ffwf-tau[tokens]')
```

If the server says it changed (a new model version, a new build) or the same text suddenly costs
much more or less, the record starts over and you are told once. `tau tokens show` lists the
records, `tau tokens show MODEL` prints one in words, and `tau tokens reset MODEL` deletes one.
`tau ext disable tokens` turns the whole thing off: nothing is read or written, and counts are
bills and bytes.

On a llama.cpp server you can have the exact number instead: `tau ext enable token_calls` asks the
server to count the request before it is sent (its own chat template, then its own tokenizer), and
that count is what the hard limit reads. It needs no extra package. By default it asks only when
the estimate cannot say whether the request fits; `tau config set token_calls.when=always` asks
before every request. Servers without a count endpoint are not asked, and a request with an image
is left to the estimate (the server counts images its own way).

The optional tokenizer package (`pip install 'ffwf-tau[tokens]'`) is looked for when τ starts, as
`extensions.tokens.tokenizers` says: `auto` (the default) uses it if it is installed and warns you
once if it is broken or from another τ version; `require` refuses to start without a working one of
the right version, naming the command that installs it; `off` never looks.

## A long conversation can be shortened

Two commands rewrite what the model reads, and both keep the old conversation: they write a new
line in the session's tree and move you onto it. Going back to an old node shows the conversation
as it was (`Ctrl+G` draws the old line as a folded branch).

`/compact [focus]` asks a model to summarize the older part of the conversation and keeps the
newest part word for word. `focus` is text the summary should keep in mind (`/compact the parser
bug`). τ also does this by itself (unless `auto` is `false`), with two limits. When a finished turn
leaves the conversation past the **soft** limit (80% of the hard one), τ compacts after the agent's
last reply, and your next message starts on the shorter line. If a turn itself grows past the
**hard** limit (the window less `reserve`), τ interrupts it just before the request that would not
fit, compacts, and carries on. If the turn began with a message of yours that the summary would
replace, that message is kept word for word after the summary:

```text
compacted 182k → ≤23k tokens (nodes 4–418 summarized as node 431; reported counts only)
```

τ counts in the unit the server bills in. What it sent already is the server's own number, from
the reply's usage. What it has not sent yet (your new message, a tool result just added, a line
it has just written) it can only bound by the bytes in it, plus 16 tokens for each message, and a
count that includes such a bound is written with `≤` and the most it can be. The bound is loose,
about three to four times the tokens of English text. So a long tool result can make τ compact
sooner than the real count would, and `/usage` ends with a line saying how counts are made:
`counts: reported only, with byte ceilings for what is not sent yet`. τ never guesses a count
from characters. Nothing has to be installed for any of this.

For that τ must know the window, so give the model a `context_window` (as for the `ctx` readout
above; a window too small to leave `reserve` at least 2 tokens is a config error). A model without
one is never compacted by itself and the status bar says so (`local:qwen has no
context_window`); `/compact` still works there too. The settings, with `/config compaction` or
`extensions.compaction` in your config:

```json
{ "extensions": { "compaction": { "reserve": 16384, "keep": 8000 } } }
```

`auto` (`true`) lets τ compact by itself, at the soft limit and at the hard one; `false` leaves
both to you and `/compact`. `reserve` is the tokens kept free for the reply and for the summary
itself (16384, or a quarter of the window if less); half of it, at most 8192, is what a summary may
take. `hard` and `soft` set the two limits directly, as token counts (`soft` above `hard` is a
config error). `keep` is the most tokens of the newest messages copied unchanged (the newest turn
is kept whole even if it is bigger). Left unset, `keep` is 20000 or half the soft limit if that is
less, which suits a 32k local model; a `keep` you set must be smaller than the soft limit. `keep`
is measured in the same counts as everything else, so with a server whose requests τ cannot yet
prove (anything but Anthropic's prompt cache) the messages are counted by their bytes, and 20000
of them are about 5000 to 6000 real tokens.
`/compact` with no `keep` set keeps at most half of the conversation, so that asking for it
summarizes something; right after a compaction it says "nothing to compact yet", since the only
thing left to summarize is a summary.

Before τ pays for a summary it checks that the line can come out under the limit: the prefix and
the request's overhead (system prompt, context files, tool definitions: it is in the first
reply's bill), the messages it would keep, and room for the summary. If not, nothing is
summarized: at the soft limit τ tells you once and goes on; for `/compact` the command fails and
says what is too big. At the hard limit the step fails the same way: τ never sends a request
that it cannot show fits. That includes a request that is over only by the bytes of what is not
sent yet (a big paste, a big tool result, counted loosely), so a message of 30,000 characters can
be refused by a 32k model that would have taken it. The error says so and names the ways past it: shorten or split the message, give the model a larger `context_window`, or turn automatic compaction off (`--set compaction.auto=false`), after which the provider decides.
After the summary is written, τ keeps the longest tail that fits beside it. A summary that does
not fit the soft limit is told once and not asked for again at the soft limit while the limits are
the same and the cursor stays on that line (a hard limit or `/compact` always tries), a notice
about a different part being too big is told as well, and a summary no shorter than what it
replaces is not written.

`model` names another model to write the summary (default: the one you are using). The summarizer's own request has to fit that model's window too: if the branch does not, τ summarizes only the older part and keeps the newest turn whole, and if even that does not fit, the step fails and says to give `model` one with a larger window. The summary is
written by a second cursor, `compaction:1`, which you can see in the tree and which `/usage`
counts; it is closed when it is done. Esc while it works leaves your conversation exactly as it
was, and so does stopping or closing the `compaction:1` cursor itself: the step that was waiting
for it fails instead of sending the request that would not fit. If the summary fails, the step
fails and says why. τ does not compact when the provider answers "prompt too long": run
`/compact`, then send again.

`/prune` needs no model. It keeps the last two turns and turns every older turn into a short
transcript at the top of the last-but-one: what you asked, how much work was left out, and the
agent's closing reply.

```text
<pruned>
User: let's do task one
[pruned: 10 messages, 44 tool calls]
Assistant: Done. I did task one. Summary: ...
</pruned>
```

In the last two turns it drops the model's reasoning and cuts any argument of a call over 200
characters to `[pruned: N chars]` (a long `edit` or `write`); in the second-to-last it also reduces
each tool result to `ok` or `error`. The last turn keeps its results. `/prune` says what it
saved (`pruned ~46k → ~6k tokens`), or `nothing to prune`. It is useful when you switch tasks, and
with a local model, where nothing is cached. Automatic pruning is not built: `prune.auto` must
stay `false`.

In `Ctrl+G` a node τ computed shows `⧉` and a copy shows `≡ (copy of 12)`; the detail pane says
where it came from and how long the conversation was before and after, and `p` jumps to the
source. `tau ext disable compaction` or `tau ext disable prune` removes either. Both end the new
line in an undo marker, so `/undo` (next section) takes you back to the conversation as it was.

## Going back: `/undo`

`/undo` takes back the last change on your branch. Right after `/compact`, `/prune` or `/clear` it
returns you to the conversation as it was before; otherwise it goes back to just before your last
message and gives the message back, so you can change it and send it again. Run it again to go
back one more step. Nothing is deleted: what you went back over stays in the tree (`Ctrl+G`), and
sending makes a new branch beside it.

```text
undid /compact: back to node 418
```

In the terminal UI, Ctrl+Z on an empty editor is `/undo`: the message goes back into the editor
as you typed it, `@file` words and all, and an undone `/compact` or `/clear` is a toast. On a cursor
that is working, `/undo` is refused (`Busy`), as every command that runs on the cursor is; Ctrl+Z
there asks first and then aborts the step, going back before your last message.

`/undo` only ever sees your own branch: a `/clear` on another cursor is not yours to undo. An
extension that changes something outside the conversation (files, say) can mark its changes for a
different `/undo`: `/undo file` takes back the last such change on the branch, if that extension is
loaded to put the files back, and a plain `/undo` passes over them. How a change is marked is in
[Conventions](conventions.md). `tau ext disable undo` removes the command; the markers then stay
in the tree as notes no model reads, and Ctrl+Z edits your last message as before.

## Starting again, branching, the tree as text, and export

The `session` built-in has four commands:

- **`/clear`** starts the conversation again in the same session: the model sees your system
  prompt and context files and nothing else, as in a new session, and keeps the model and options
  this branch was using. The old conversation stays in the tree, and `/undo` goes back to it. The
  new conversation starts from the session's own system prompt, so the first request after
  `/clear` is the same request a new session would send, and a provider's prompt cache stays
  warm.
- **`/branch [label]`** opens a second cursor where you are, to try something else from here
  (Alt+Right views it in the terminal UI).
- **`/tree`** draws the session's tree as text, for a client with no tree screen of its own:

  ```text
  0 config: {"model": "local:qwen"}
  1 system: You are τ, a coding agent…
  ├─ 2 user: fix the parser
  │  3 assistant: Done. [2 calls]
  └─ 9 undo: /clear
     10 user: now the lexer  ◀ 0
  ```

  `/tree at=9` draws only what is below node 9.
- **`/export [path]`** writes your conversation as Markdown, as `tau export` does: to a file
  (`/export notes.md`, relative to the working directory; a file that exists is refused, not
  overwritten) or as the command's answer;
  `thinking=true` includes the model's thinking.

There is no `/new`: starting a new session is your client's to do (Ctrl+N in the terminal UI).
`tau ext disable session` removes all four:

```bash
tau ext list | grep -A1 -E '^(session|undo) '
```

```text expect
session       on         built-in
              commands: /clear, /branch, /tree, /export; tools: none
--
undo          on         built-in
              commands: /undo; tools: none
```

## Ask before a tool runs

Permissions is an example guardrail for the built-in file tools (`read`, `write`, `edit` and
`grep`, with `find`). It catches accidents: a model that wanders into `/etc`, or edits a file you
meant to keep. It is not a security boundary, and `bash` is out of its scope: a shell has endless
ways to say the same thing, so a pattern over a command's text would promise a guard it is not, and
`bash` rules name the tool only (or, to allow, one exact command). Real
enforcement belongs below the agent, at the operating-system level: run τ as a separate user with
only the access you want it to have, in a mount namespace or container that does not show the
files, or under Landlock.

Out of the box τ runs whatever tool the model calls and asks nothing, as it always has. Write rules
and it asks. A rule is a tool's name, or a tool's name, a colon and a pattern. There are three
lists, and a call is checked against `deny` first, then `allow`, then `ask`; a call that no rule
names runs:

```json
{
  "extensions": {
    "permissions": {
      "ask": ["bash", "write", "edit"],
      "allow": ["bash:git status", "bash:git diff"],
      "deny": ["write:/etc/*", "edit:/etc/*"]
    }
  }
}
```

That asks about every `bash`, `write` and `edit` call except the exact commands `git status` and
`git diff`, and refuses outright any write under `/etc`. The one line that turns asking on is
`/config permissions ask=bash,write,edit --save`.

The pattern is a shell-style glob over the `path` argument (for `read`, `write`, `edit`, `grep` and
`find`): `*` matches anything, including `/` and line breaks,
`?` one character, `[ab]` a set, and capitals are not lowercase. `fetch:GLOB` is the same kind of rule over
the host of the URL ([below](#reading-the-web-and-searching-it)). A tool an extension gives the model
takes only its bare name (and a name may hold a colon: `srv:tool`). A rule that names a tool nobody
provides (`Bash`, `wirte`) is refused when you set it: `/config permissions ...` and `tau config
set` check the names, and a bad rule is never written, `--save` or not. A file that already holds
one (an extension that provided the tool was deleted, or you edited the file) only warns at the
start and in `tau config check`, and the rule matches nothing; `tau config check --clean` removes
it. A tool of a built-in extension that is off (`agent`, until you enable `agents`) is known.

Paths are matched as the model wrote them and as the file they name: relative to the session's
directory, with `..`, `//` and symlinks resolved. So `write:/etc/*` also refuses `../../etc/x` and a
link that lands in `/etc`. A rule that starts with a plain relative name (`read:src/*`,
`write:./out/*`) starts in the session's working directory, as the tool's own paths do, so
`allow: ["read:src/*"]` allows `src/a.py` and nothing outside that directory. (`tau config check`
reads the rule as written and has no working directory to resolve it against, so it says nothing
about it.) Relative means relative to the session's directory, for `deny`, `ask` and `allow` alike,
not to the config file's directory. A rule that starts with `/` or a wildcard is used as it is. `grep` also names each of its
`files` (and the working directory when it names no path): a `deny` or `ask` fires if any of them
matches, an `allow` needs all of them to, and an `allow` matches the resolved path only.

An `ask` for a search works the other way: `ask: ["grep:/etc/*"]` asks before any `grep` rooted at
or inside `/etc`, and before one that holds it (`path=/`), since the search could read there.
(A pattern that starts with a wildcard has no directory to compare, and asks only for the paths a
search names.)

`grep` and `find` read everything under their root, so a `deny` for them is applied to what they
find too. `deny: ["grep:/etc/*"]` refuses a search that is rooted inside `/etc` (`path=/etc/ssh`, or
a link into it), or that names a file there. A search that only *holds* `/etc` (`path=/etc`, `path=/`,
no path in a directory that contains it, a link to it) runs, but leaves out every file the pattern
matches, including a file reached through a link, and ends its answer with a line such as `[2 path(s)
left out of this search: they match its exclude patterns]`. A directory that matches is not
entered, so nothing inside it is read or named in an error. The model is told because an answer
that silently lacks files reads as "no match", and it would conclude wrongly; the line gives no
names or contents. A `read` deny with a pattern (`read:/home/me/.ssh/*`) guards what `grep` reads
in the same way; `find` lists names only, so it follows its own `find` rules. Every search
of the tool is given its deny patterns, since a link under any root can lead to a denied file;
nothing is added to the answer unless a file was left out. A rule that starts with a wildcard
(`grep:*secret*`) works the same way. The files are left out of `grep`'s overflow file too.

Commands are different, because a glob is not a shell: `git status*` would also allow
`git status; rm -rf x`, and `rm *` would miss `/bin/rm x`, `sudo rm x` and `bash -c "rm x"`. So a
`bash` rule in `deny` or `ask` is the bare name, `bash`, and an `allow` rule is `bash` or one exact
command (`bash:git status` allows `git status` and nothing else, not even `git status -s`; write a
literal `*`, `?` or `[` as `[*]`, `[?]`, `[[]`). A `bash` rule with a wildcard is refused where it is
configured. **`ask: ["bash"]` puts every command in front of you**. If you want finer rules over
commands, an extension of your own can give them (a `pre_tool` hook sees the command); rules guard
against the model, they are not a sandbox.

A call that a rule says to ask about holds the cursor with a question that names the tool, its
command or path, and the rule that matched:

```text
bash: git push origin master
(ask rule: bash)                                  yes / always / no
```

`yes` runs it. `always` runs it and writes its exact rule (`bash:git push origin master`, or for a
file tool the resolved path) into the `allow` list of your config file, so it is never asked again in
any session, and τ tells you it did. A call whose arguments are malformed (a `read` with no `path`)
has no exact rule: `always` runs it once, saves nothing, and says so, because the bare tool name
would allow every call. If a project layer or a `--set` also sets `allow` and hides the
file's list, τ says so and the rule still holds for the rest of the run. `no` gives the model
`the user refused this call`. Esc, or going to another node, refuses it too. A batch of calls asks
all its questions at once, and a [sub-agent](#sub-agents)'s questions are asked on its own
cursor. Another client of the same session (`tau serve`) can answer a question; the first answer
wins.

`tau -p` has no one to ask, so it never decides a question by itself: one it has no rule for is
shown (`tau: question 0 from permissions on cursor 0 has no rule: ...`) and the run stops, exit 3.
Give it answers with `--answer`, which matches who asked and what, and declines with `--decline`
(the call is refused, and the run goes on). The first rule that matches wins:

```bash skip
tau -p --answer permissions=yes "tidy the imports"
tau -p --answer 'permissions:bash: make test=yes' --decline permissions "fix the test"   # refuse the rest
```

Attached to a `tau serve`, a question with no rule stays open for the server's other clients (a
person in the terminal UI); `tau -p -c --answer ...` answers the questions already open on its
cursor before it sends.

A `--answer` text is a glob over the call (a file tool's question shows the resolved path, so a text
for `/proj/*` does not answer `/proj/../etc/x`). For `bash` it is as an `allow` rule is: `bash`
alone answers every command, and `bash: git push origin master` that one command exactly (it may
also be written as a rule, with no space: `bash:git push origin master`); a `bash` text with a
wildcard is a usage error. Other tools' arguments are no shell command, so a `;` in them means
nothing. Leave the
text out (`--answer permissions=yes`) to answer every question. An empty pattern (`bash:`) stops the
start, and `tau config check` names it.

## Sub-agents

A sub-agent is a second model run the main one starts for a job of its own. It is on by default,
so the model has one more tool, `agent`, in every request; if you do not want to pay for its
description in every prompt, turn it off:

```bash
tau ext list | grep -A1 '^agents'
tau ext disable agents
tau ext list | grep -A1 '^agents'
tau ext enable agents
```

```text expect
agents        on         built-in
              commands: none; tools: agent
set load.agents = false in <*>
agents        off        built-in
              load.agents = false in <*>
set load.agents = true in <*>
```

The model has a tool, `agent(task)`. Each call opens a new cursor, labelled `agent:0:1`,
`agent:0:2`, and so on (`agent:P:N`: `P` is the cursor that made the call, `N` counts its agents),
that starts from the system prompt and your context files and nothing
else, so the model must put everything the job needs in `task`. The result the model reads is
`agent:0:1 answered:` and the agent's last reply. The agent is closed when it has answered; it stays in
the tree (`Ctrl+G`) as a dimmed closed cursor with its label. Several `agent` calls in one reply
run at once. To give an agent a second job, the model calls `agent(task, to="agent:0:1")`, which
reopens it with its earlier work still in its context. An agent that a crash or a stopped server
left open, at rest, is continued the same way; one that is still working is `agent:0:1 is still
working`, and `to=""` is an error, not a new agent. A final reply with no text is `agent:0:1 gave an
empty reply`.

An agent has every tool except `agent`, or exactly the `tools` you list (a call to any other tool
fails with `tool not offered`), so they do not nest, and the step limit bounds each one.
Its replies are counted by `/usage`, and Esc on the main cursor aborts the agents it started.
Permission rules apply to an agent's calls as to any other. If an agent fails, the call fails with
`agent:0:1 failed:` and the reason. The settings:

```json
{ "extensions": { "agents": { "model": "local:small", "tools": ["read", "grep", "find"] } } }
```

`model` is the model agents run on (default: the one the calling branch is on), and `tools` the
tools they are offered (default: all but `agent`). A name in `tools` that no loaded extension
provides is refused by `/config` and `tau config set`, and is a warning (a name that matches
nothing) when a file already holds it. A list typed on a command line is
split at commas and each item trimmed, so `tools=read, grep` is `["read", "grep"]`.

## Plan before it acts

`/plan` puts the branch in plan mode, and nothing in a session changes until you type it. In plan
mode the model is told to research and propose a plan, and is offered only tools that read:
`read`, `grep`, `find`, and `bash` for a short list of read-only commands (`ls`, `pwd`, `cat`,
`head`, `tail`, `wc`, `git status`, `git diff`, `git log`), and only when the command chains,
redirects or substitutes nothing. A call to anything else is refused, and the model is told to
put the change in its plan. Every client shows the line `plan mode` for the cursor while it is on.

```text
> /plan add retry to the fetch tool
plan mode on: read-only tools; you will be asked to approve the plan
```

When the model's reply ends the plan, τ asks `Approve this plan?`: `approve` leaves plan mode,
tells the model to carry the plan out and lets it run on by itself, `revise` asks what should
change and sends it, and `reject` leaves plan mode. An empty answer, or nobody connected, stops
and leaves plan mode on. Under `tau -p` nobody is there to answer: without an `--answer` rule
the run prints the plan, says `question 0 from plan_mode ... has no rule` on standard error and
exits 3 (`--answer plan_mode=approve` approves it). `/plan` takes the rest of the line as the task, with no quotes.

Plan mode is a setting of the branch (`/config plan_mode mode=act` leaves it by hand), so going
back to a node from before `/plan` leaves it, and a second cursor is unaffected. It is a guard,
not a sandbox: `git diff --output=f` writes a file. The lists of tools and commands are not
options, because a `/config` from any client could widen them; to change them, write a drop-in
named `plan_mode` (it shadows the built-in) or turn the built-in off with
`tau ext disable plan_mode`.

## Ask before a command prints a lot

The bash guard is off until you turn it on (`tau ext enable bash_guard`). It asks before a `bash`
call that tends to print a lot: `find /`, `cat` of a log or an archive, `npm install`,
`cargo build`, a `git log` with no count, a recursive `grep` without `-l`, `journalctl` without
`-n`, `docker logs` without `--tail`. A command already piped to `head`, `tail` or `wc` is never
asked about. The question has four answers:

- `reject` refuses the call and tells the model to narrow it;
- `allow` runs it once;
- `always` runs it and adds the pattern to `allow` in your config file, for every session;
- `truncate` asks for a `grep` pattern (decline for none) and runs the command with its output
  piped through `grep` and `tail -n 200`. The model is told what really ran (the result starts
  with `[bash_guard ran: ...]`).

The options are what you may tune: `patterns` (the built-in ones are the default, so
`/config bash_guard` shows them; a pattern with a comma is typed as a JSON array,
`patterns='["a{1,2}"]'`), `allow`, `unattended` (`reject`, `allow` or `truncate`, what happens
when nobody answers) and `wait` (seconds to wait for an answer, 600 by default). A question
nobody answers within `wait` seconds is decided by `unattended`; an abort never runs the command.
`tau -p` does not wait: a question it has no `--answer` or `--decline` rule for stops the run at
once, exit 3.

## Choose the model by the prompt

The router is off until you turn it on (`tau ext enable router`). It does two things. `!name text`
sends that one message on the model `name` (any configured model; `!name` is stripped from what the
model reads), and a table in the options maps the text of a message to a model: the first regex
that matches decides.

```json
{
  "extensions": {
    "router": {
      "when": ["```", "(?i)\\b(prove|derive)\\b", "^.{0,24}$"],
      "use": ["local:coder", "anthropic:opus", "local:small"]
    }
  }
}
```

A switch writes a `Config` node and a `Custom` node of kind `route` saying why (`prefix`, or the
regex), both before your message, and says `router: local:coder (/```/)` to the client that typed
it. Nothing is written when the branch is on that model already. `once` puts the branch back on
the model it was on before the router first switched it, after the turn's last reply. `/route off`
(or `/config router enabled=false`) stops the table and leaves the prefix. A `!name` that is not a
configured model is not a prefix: it goes to the model as typed.

## Tools from MCP servers

An MCP server (Model Context Protocol) is a program, or a web service, that offers tools: GitHub,
a database, a browser. `mcp` gives the model the tools of the servers you list. It is off until you
turn it on, and with no server listed it does nothing:

```bash skip
tau ext enable mcp
```

List the servers in `$TAU_HOME/mcp.json` (`~/.tau/mcp.json`), in the shape other MCP clients use,
so a server's README snippet goes in as it is:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}" }
    },
    "docs": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer ${DOCS_TOKEN}" }
    }
  }
}
```

A server with a `command` runs on your machine and speaks over its stdin and stdout; one with a
`url` is reached over streamable HTTP (the older SSE transport, `"type": "sse"`, is not supported).
`${NAME}` is replaced from your environment, or from a trusted project's `.env`, so the token stays
out of the file; `${NAME:-default}` gives a default. A server whose name is not set and has no
default is skipped, with a warning that says which (see below).

A project can list servers too, in `.tau/mcp.json` or in `.mcp.json` at its root (the file Claude
Code reads), so a project set up for Claude Code works as it is. Both are pinned by
[trust](configuration.md#projects-and-trust): `tau trust` shows each server and what it runs (the names of its `env` and
`headers`, never their values), and a change to either file asks again. For one server name the
nearest file wins: `.tau/mcp.json`, then `.mcp.json`, then yours. `"disabled": true` turns a
server off, and `{"mcpServers": {"github": {"disabled": true}}}` in `.tau/mcp.json` turns off a
`.mcp.json` server you do not want, without editing that file.

A program that starts τ with servers of its own (an editor, through tau-acp) names its files in
the environment variable `TAU_MCP_CONFIG` (several separated by `:`, `;` on Windows), and turns
`mcp` on for that run: `TAU_MCP_CONFIG=/run/editor-mcp.json tau serve --set load.mcp=true`. Those
files are nearest of all, so their servers win a name, and a file named there must exist. It is
set by whoever starts τ, like `--set`: no client can set it.

The model sees each tool as `mcp__SERVER__TOOL` (`mcp__github__create_issue`). They are asked
about like any other tool, and a rule that names the server names every tool it has:

```json
{ "extensions": { "permissions": { "ask": ["mcp__github"], "allow": ["mcp__github__get_issue"] } } }
```

When τ starts it starts every server at once and waits until each has listed its tools, so the
first message already has them. **A server that does not start is skipped, and τ starts without
it**: the others work. It is said on the terminal as τ starts, with why and the last line the
server wrote to its error output:

```text
tau: mcp: MCP server github exited (status 1): Error: GITHUB_PERSONAL_ACCESS_TOKEN not set; τ starts without it
```

Every client also gets it as an error notice (the TUI shows it; a client that connects later still
gets it), and `/mcp` lists the server as `failed:` with the reason. A configuration τ cannot use
(a file that is not JSON, a server with no `command`, an unset `${NAME}`) is skipped and reported
the same way, as a `warning:` line. To stop hearing about a server, fix it, or add
`"disabled": true` to it.

τ gives the servers 30 seconds, less a moment to stop the slow ones; a server still starting then
is skipped. The first `npx` or `uvx` run of a server downloads it and may take longer, so give `mcp`
more time in your config (`{"extensions": {"mcp": {"start_timeout": 120}}}`), or run the command
once by hand first.

While τ runs, `/mcp` lists the servers, whether each is running and their tools. A server that
lists new tools has them offered from the model's next request. A server that exits is reported
(an error notice) and its tools answer an error until you restart τ. Esc cancels a call at the
server. A tool's text and images reach the model; other kinds of content are named and left out.
Prompts and resources that a server offers are not used.

## Reading the web and searching it

`fetch` is on from the start and needs nothing: the model gives it a URL and gets the page as
text. `search` is off, because it needs an account with a search service and its key. Both tools
mark what they bring back as untrusted web content, once, ahead of it: a page is data, and a
page that says "ignore your instructions" is still only text.

```bash
tau ext list | grep -A1 '^fetch'
```

```text expect
fetch         on         built-in
              commands: none; tools: fetch
```

### `fetch`

`fetch(url, max_chars=20000, start=0)` makes one GET, with a `User-Agent` that says what tau is, an
`Accept` that asks for Markdown first, and these limits (they are fixed: a model's client could
not change them): 10 s to connect, 60 s in all, 5 MiB of body, 20 MiB for an image, and 5
redirects.

- **A web page** comes back as Markdown: its main content (the `main` element, or the one
  `article`, else the body) with headings, lists, quotes, code, tables as rows, and links made
  absolute. Navigation, footers, sidebars, scripts, styles and hidden elements are left out. A page
  that is empty until a script fills it has no text for tau to read, and the call says so; τ runs no
  JavaScript.
- **Text**, JSON, XML and the like come back as they are, decoded by their charset. A PDF or another
  binary type is an error naming the type; tau reads text and images only.
- **An image** (PNG, JPEG, GIF, WebP) comes back as an image if the model takes images (`images:
  true` in its config, within its provider's limits, as for `read`); otherwise as one line saying
  what it is.
- **A long page** is cut at `max_chars` (at most 200000), and the first line says which characters
  are there and `call again with start=N for more`. Each call fetches the page again.
- **A redirect** to the same host is followed. One to another host is not: the answer is the URL it
  names, and the model calls `fetch` again. That is what makes the permission rules below mean
  something: every host the model reads from is a call they see.
- An error status, a timeout and a failed connection are errors naming what happened.

A URL whose host is, or resolves to, a loopback, private, link-local or other non-public address
(`localhost`, `192.168.1.5`, `169.254.169.254`) is refused, a redirect's too: a page's link, or a
model led by one, cannot reach a service on your own network. If you want that, for documentation
you serve yourself, say so in the config file. This is the administrator's setting, not an option,
so no client can turn it on:

```json
{ "extensions": { "fetch": { "allow_private": true, "timeout": 30 } } }
```

(`timeout` is the whole call, in seconds, 1 to 600.) The check looks the name up before the
request, and the request looks it up again, so a name that answers differently the second time
gets through: this guards against the model and the pages it reads, and is not a sandbox. `tau ext
disable fetch` takes the tool away.

**Permissions** know it as `fetch`, and `fetch:GLOB` matches the URL's host, ignoring case (not the
scheme, port or path), with the same glob as for paths: `*` crosses dots, so `*.python.org` is every
host under `python.org` and not `python.org` itself. The host is read as the request reads it:
`https://docs.python.org@evil.com/` is a visit to `evil.com`.

```json
{
  "extensions": {
    "permissions": {
      "allow": ["fetch:docs.python.org", "fetch:*.wikipedia.org"],
      "ask": ["fetch"]
    }
  }
}
```

That reads two documentation sites freely and asks about any other host; answering `always` writes
`fetch:HOST` for that host. A `deny` rule works the same way.

### `search`

`search(query, limit=5)` asks a search service for up to `limit` (1 to 20) results and gives the
model a numbered list: title, address and a snippet. The model reads a result with `fetch`.

```bash
tau ext list | grep -A1 '^search'
```

```text expect
search        off        built-in
              off (default); to turn it on: tau ext enable search
```

The provider, its key and its address are the administrator's, in the config file and the
environment, and no client can read or change them. The key is never in a config file: the setting
`api_key_env` names the variable that holds it. Turning the tool on without the key stops the start
and says which variable is missing:

```text
extension "search" failed to load; tau did not start.
  source: built-in search
  ConfigError: the search tool is on and the brave provider needs an API key, but the environment
  variable BRAVE_API_KEY is not set. Set it in the environment, or in a trusted project's .env file
  to start without it: tau ext disable search
```

| `provider` | service | key | `base_url` |
|---|---|---|---|
| `brave` (the default) | [Brave Search API](https://brave.com/search/api/) | `BRAVE_API_KEY` | optional |
| `marginalia` | [Marginalia](https://about.marginalia-search.com/article/api/), an index of the non-commercial web | `MARGINALIA_API_KEY` (the shared key `public` works but is often rate limited) | optional |
| `searxng` | a [SearXNG](https://docs.searxng.org/) instance you run, with its JSON format enabled | none | required |

```json
{
  "load": { "search": true },
  "extensions": {
    "search": { "provider": "searxng", "base_url": "http://127.0.0.1:8080" }
  }
}
```

The settings are `provider`, `api_key_env` (the variable that holds the key, if it is not the one in
the table) and `base_url` (a proxy, or your instance). A setting that is not one of these, `api_key`
included, stops the start and names itself. A rejected key and a rate limit come back as tool errors
that say so, and never contain the key. Permissions know the tool as `search`, by its bare name.

## Run your own commands at hook points

`hooks` runs shell commands when something happens in a session: before a tool runs, after it
ran, when you send a prompt, when the model finishes its turn. It follows the convention Claude
Code, Codex and Gemini CLI share, so a hook script written for one of them can run here: the
command is given the event as JSON on its standard input, and answers with its exit code or a JSON
object on its standard output. An exit code of 2 blocks, and what the command wrote to standard
error is the reason.

A hooks file runs commands with your authority, so the extension is off until you turn it on:

```bash
tau ext enable hooks
tau ext list | grep -A1 '^hooks'
```

```text expect
set load.hooks = true in <*>
hooks         on         built-in
              commands: /hooks; tools: none
```

The commands are in `hooks.json` in your `$TAU_HOME`, and in `.tau/hooks.json` of a project you
have trusted. The file is the `hooks` key of a Claude Code `settings.json`, so a block written for
it can be pasted in. This one adds a line of context to every prompt you type:

```bash
cat > context.sh <<'SCRIPT'
#!/bin/sh
cat > /dev/null                 # the event; this command has no use for it
echo "branch: main"             # standard output is added to the prompt
SCRIPT
chmod +x context.sh
```

```json file=$TAU_HOME/hooks.json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "./context.sh" }] }
    ]
  }
}
```

```bash
tau -p "what is the plan?"
```

```text expect
mock: what is the plan?

branch: main
```

`/hooks` lists the commands a session has. The files are read when τ starts, so edit one and start
τ again; a file τ cannot use stops the start, naming each problem.

A command that exits 2 refuses the message, and `tau -p` stops with its reason (a relative command runs in the session's directory):

```bash
cat > friday.sh <<'SCRIPT'
#!/bin/sh
cat > /dev/null
echo "no prompts on Fridays" >&2
exit 2
SCRIPT
chmod +x friday.sh
echo '{"hooks": {"UserPromptSubmit": [{"hooks": [{"command": "./friday.sh"}]}]}}' \
  > "$TAU_HOME/hooks.json"
tau -p "hello" 2>&1 || echo "exit $?"
```

```text expect
tau: no prompts on Fridays
tau: stopped before finishing: halted: no prompts on Fridays
exit 3
```

### Events

| event | when | a block (exit 2, or `decision`) | the command is also given |
|---|---|---|---|
| `PreToolUse` | before a tool runs (`pre_tool`) | refuses the call; the reason is the call's error, which the model reads | `tool_name`, `tool_input`, `tool_use_id` |
| `PostToolUse` | after a tool succeeded (`pre_result`) | cannot undo it: the reason is added to the result, after its text, as `hook feedback: ...` | `tool_name`, `tool_input`, `tool_response` (the result's text), `tool_use_id` |
| `PostToolUseFailure` | after a tool failed, or the call was refused | the same, added to the error | `tool_name`, `tool_input`, `error`, `tool_use_id` |
| `UserPromptSubmit` | a person's message, before it is written (`pre_input`) | halts the input: the reason is a notice, and the message stays pending | `prompt` |
| `Stop` | a person's turn ended: the model's reply asks for no tool (`post_step`) | the turn goes on: the reason is sent to the model as input | `stop_hook_active`, `last_assistant_message` |
| `SessionStart` | τ starts the session | blocks nothing | `source` (always `startup`) |
| `SessionEnd` | τ shuts down | blocks nothing | `reason` (always `other`) |

Every event is also given `hook_event_name`, `cwd` and, where there is one, `cursor` (the cursor's
id). `tool_input` is the call's arguments as the tool takes them: τ's `bash` takes `command`, as
Claude Code's does, but its file tools take `path`, not `file_path`. A call whose arguments are not
a JSON object has `tool_input: null` and the text in `tool_input_raw`. The command runs in the
session's directory, with `TAU_PROJECT_DIR` and `CLAUDE_PROJECT_DIR` set to the trusted project's
root (or that directory), and `TAU_HOOK_EVENT` to the event.

`UserPromptSubmit` runs for what a person typed, not for what an extension sent (a sub-agent's
task, a summary's prompt), and `Stop` only ends a person's turn, so a sub-agent finishing never
runs it. After a `Stop` command has kept one turn going 8 times in a row, τ ends the turn and says
so; it also says so, and ends the turn, when auto-step is off.

### The file

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "bash",
        "hooks": [{ "type": "command", "command": "~/hooks/no-push.sh", "timeout": 10 }]
      }
    ],
    "Stop": [{ "hooks": [{ "type": "command", "command": "make test", "onFailure": "continue" }] }]
  }
}
```

- **`matcher`** (the three tool events only): empty, `*` or none is every tool. Letters, digits,
  `_`, `-`, spaces, `,` and `|` only is a list of exact tool names (`bash|write`). Anything else
  is a regular expression searched in the name (Python's `re`, not JavaScript's: `^(read|grep)$`).
  Claude Code's names for τ's base tools match too: `Bash`, `Read`, `Write`, `Edit`, `Grep` and
  `Glob` (τ's `find`). The `tool_name` a command is given is always τ's own, in lower case.
- **`command`** runs through `/bin/sh -c`. With **`args`** (a list of strings) it is the program,
  run with those arguments and no shell.
- **`timeout`**, in seconds, default 60 (20 for `SessionStart`, 4 for `SessionEnd`).
- **`onFailure`**: `block` (the default) or `continue`, below.

The commands of one event run one after another, in file order, your file first and then the
project's (a repeat of one runs once). The first one that blocks stops the rest, and a command
sees the arguments an earlier one rewrote. Claude Code runs them at once; τ does not, so that what
one rewrites is the same on every run.

### What a command answers

- **Exit 0** with nothing, or with text that is not JSON: go on. The text is added to the message
  at `UserPromptSubmit`; at `SessionStart` τ says on standard error that it was dropped; at the
  other events it is not used.
- **Exit 0** with a JSON object: the fields below.
- **Exit 2**: a block. Standard error is the reason (`exited 2 with nothing on standard error`
  when there is none). At `SessionStart` and `SessionEnd`, which cannot be blocked, it is a failure.
- **Any other exit code**, 1 included: a failure.

A JSON object may hold these; its `hookSpecificOutput` must say `"hookEventName"`, the event.

| field | at | does |
|---|---|---|
| `hookSpecificOutput.permissionDecision`: `deny` | `PreToolUse` | blocks, with `permissionDecisionReason` |
| `hookSpecificOutput.permissionDecision`: `ask` | `PreToolUse` | asks the person `yes` or `no` before the call runs, with the reason in the question; `tau -p` needs `--answer hooks=yes` |
| `hookSpecificOutput.permissionDecision`: `allow` | `PreToolUse` | no objection, and nothing more: it does not skip the `permissions` rules |
| `hookSpecificOutput.updatedInput` | `PreToolUse` | the arguments the tool runs with, whole; the `permissions` rules see these |
| `decision`: `block`, with `reason` | `PreToolUse` (also `approve`), `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop` | a block, as exit 2 |
| `hookSpecificOutput.additionalContext` | `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit` | text added after the result, the error or the message (at most 10000 characters) |
| `hookSpecificOutput.updatedToolOutput` | `PostToolUse` | the text of the result the model reads instead |
| `systemMessage` | any | a warning notice, for the person |
| `suppressOutput` | any | accepted; it means nothing here, as in Claude Code |

**Anything else is a failure that names the command and the field**: τ does not drop a decision it
cannot honor. That is `continue: false` (and `stopReason`), `defer`, `updatedMCPToolOutput`,
`sessionTitle`, `suppressOriginalPrompt`, `initialUserMessage`, any other key, and
`additionalContext` where the table does not list it (`PreToolUse`, `Stop`, the session events).

### When a command fails

A command that does not start, runs past its `timeout` (it is killed, and what it started with
it), writes more than 1 MiB to a stream, exits other than 0 and 2, or answers with JSON τ cannot
use has **failed**. It is always told to you, as an `error` notice from `hooks` that names the
command and why: `hook './guard.sh' timed out after 10 s and was killed`.

What else it does is `onFailure`. With `block`, the default, a failure at a gate (`PreToolUse`,
`UserPromptSubmit`) also refuses the call or the message, with that text as the reason: a guard
that cannot run does not let things through. With `continue`, it only reports. At the other events
there is nothing to refuse, and it only reports; a `SessionStart` or `SessionEnd` failure is
written to standard error, since no client is connected yet, or any longer. Claude Code lets a call
through when a command exits 1; τ does not, because a command that is broken has not approved.

### The project's file, and order

`.tau/hooks.json` in a project is pinned by `tau trust` like `.tau/extensions/`, because it runs
commands: the report shows the whole file, never part of it, and a change to it asks again. τ uses
the bytes you approved. A file that is not a hooks file cannot be approved.

At `pre_input` and `pre_tool` the `hooks` commands run before the other built-ins' hooks
(`pre_input` at -100, `pre_tool` at -150), so a command can rewrite a call's arguments before
`permissions` judges them. The ones that only see what happened (`PostToolUse`, `Stop`) run last.
`tau ext list --hooks` shows the order.

### What is not supported

τ has no hook point for most of Claude Code's other events: `Notification`, `PermissionRequest`,
`SubagentStart` and `SubagentStop`, `PreCompact` and `PostCompact`, the `Task*` events, and the
rest. A hooks file that names one is refused when τ starts, naming it. So is a handler that is not
`type: "command"` (`http`, `mcp_tool`, `prompt`, `agent`), a handler key `if`, `async`,
`asyncRewake`, `shell`, `statusMessage` or `once`, and a matcher on an event that has no tool. The
commands are not given `session_id`, `transcript_path`, `permission_mode` or `effort`, and a
`SessionStart` command cannot add context to the conversation.

**Windows.** `hooks` is not available there: it runs commands through a POSIX shell, which τ does
not have on Windows (the `bash` tool is unavailable for the same reason). Turning it on stops the
start with that said.
