# What τ does for you

Five things happen in a session without any extension of yours: a failed request is tried again,
`@file` in a message attaches the file, files you write become commands (prompt templates and
skills), what the session has used and spent is shown, and a long conversation can be shortened
(`/compact`, `/prune`). Two more wait for you to ask: rules that ask before a tool runs
(permissions), and sub-agents, which are off until you turn them on. Each can be switched off, and
each is a few lines to use. The examples run with the mock
model, which needs no server and no key.

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
the file. A file named `config` is a start error: `/config` is the built-in command.

`tau -p "/review main.rs"` prints the model's reply, after the command's own result if it has one,
as `tau -p "review main.rs"` would.

### The files

| where | what |
|---|---|
| `$TAU_HOME/prompts/NAME.md` | a template |
| `$TAU_HOME/skills/NAME/SKILL.md` | a skill |
| `.tau/prompts/NAME.md`, `.tau/skills/NAME/SKILL.md` in a trusted project | the same, for the project; for one name the project's wins |

For one name, **a project's file hides the user's**: if `.tau/prompts/review.md` and
`$TAU_HOME/prompts/review.md` both exist in a trusted project, `/review` is the project's, and
nothing says the user's was hidden. A project's skill hides a user's template of that name too.

A name is ASCII letters, digits, `_` and `-`. A template and a skill with one name in one
directory, a bad name, a file that is not UTF-8 and a skill without a description each stop the
start with a message naming the file, because a command that quietly went missing would be harder
to find. So does a file in the wrong place: `skills/pdf/skill.md` (the file is `SKILL.md`,
in capitals), a flat `skills/pdf.md` (a skill is a directory), `skills/group/pdf/SKILL.md` (skills are not
read from subdirectories), `prompts/sub/x.md` (templates are
not read from subdirectories) and `prompts/x.MD` (`.md` in lower case). All the problems are listed
at once, and `tau trust` refuses a project whose files have any, listing all of them.
`tau ext disable skills` starts without them. A byte order mark at the start of a file is ignored.

A file may begin with frontmatter, `key: value` lines between two `---` lines. Read are
`description` (one line), `argument-hint`, `disable-model-invocation` (`true` or `false`) and
`name` (which, if given, must be the file's name). Other keys, comments and indented lines are
ignored, so a Claude Code skill loads as it is. There is no YAML: `description: >` is refused,
not misread.

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
a model that has a `price`, and `ctx 7%` for one that has a `context_window`: the share of the
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
      "price": { "input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75 }
    }
  }
}
```

A price is US dollars per million tokens, and all four fields are required: a model that does not
charge for writing the cache repeats its `input` price as `cache_write`. A price with a field
missing, below 0, or not a number is a config error naming the model. Groups hand a price down like
any setting, and a project can change one field (`"price": {"output": 20}`). A fifth field,
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

Cost is worked out when you look, from the prices in the config the session was started with:
correct a price and start the session again (`tau -c`, `tau -r`), and the whole session is
priced anew. A reply is priced by the model it was sampled with, so a session that changed models
half way adds up right. The sums are exact, not floating point. A session made with `--fork`
starts with a copy of the source's walk, and its `/usage` counts the replies it copied too.

When a reply reads nothing from the prompt cache (the server said `cached: 0`) soon after a reply
of the same model that should have written it, τ tells you once per model, with the size of the
prompt that went at full price. It is about money, so only a model with a `price` gets it.
`--set usage.cache_notice=false` silences it, and
`tau ext disable usage` removes all of this.

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
source. `tau ext disable compaction` or `tau ext disable prune` removes either.

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
`?` one character, `[ab]` a set, and capitals are not lowercase. A tool an extension gives the model
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

A sub-agent is a second model run the main one starts for a job of its own. It is off until you turn
it on, because the tool it adds would be in every request:

```bash
tau ext list | grep -A1 '^agents'
tau ext enable agents
tau ext list | grep -A1 '^agents'
tau ext disable agents
```

```text expect
agents        off        built-in
              off (default); to turn it on: tau ext enable agents
set load.agents = true in <*>
agents        on         built-in
              commands: none; tools: agent
set load.agents = false in <*>
```

With it on, the model has a tool, `agent(task)`. Each call opens a new cursor, labelled `agent:1`,
`agent:2`, and so on, that starts from the system prompt and your context files and nothing
else, so the model must put everything the job needs in `task`. The result the model reads is
`agent:1 answered:` and the agent's last reply. The agent is closed when it has answered; it stays in
the tree (`Ctrl+G`) as a dimmed closed cursor with its label. Several `agent` calls in one reply
run at once. To give an agent a second job, the model calls `agent(task, to="agent:1")`, which
reopens it with its earlier work still in its context. An agent that a crash or a stopped server
left open, at rest, is continued the same way; one that is still working is `agent:1 is still
working`, and `to=""` is an error, not a new agent. A final reply with no text is `agent:1 gave an
empty reply`.

An agent has every tool except `agent`, or exactly the `tools` you list (a call to any other tool
fails with `tool not offered`), so they do not nest, and the step limit bounds each one.
Its replies are counted by `/usage`, and Esc on the main cursor aborts the agents it started.
Permission rules apply to an agent's calls as to any other. If an agent fails, the call fails with
`agent:1 failed:` and the reason. The settings:

```json
{ "extensions": { "agents": { "model": "local:small", "tools": ["read", "grep", "find"] } } }
```

`model` is the model agents run on (default: the one the calling branch is on), and `tools` the
tools they are offered (default: all but `agent`). A name in `tools` that no loaded extension
provides is refused by `/config` and `tau config set`, and is a warning (a name that matches
nothing) when a file already holds it. A list typed on a command line is
split at commas and each item trimmed, so `tools=read, grep` is `["read", "grep"]`.
