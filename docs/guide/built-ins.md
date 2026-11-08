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
off; it skips only installed packages and drop-ins. `load` is read from the config files only, so
no flag turns retry off for a single run (`--set load.retry=false` says so).

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
any setting, and a project can change one field (`"price": {"output": 20}`).

A reply that was aborted before the server reported usage says so, and `/usage` counts it apart
("1 reply was aborted and reported no usage"). A reply whose usage could not be read says why, on
this line and in `/usage`. Counts are written `812`, `12.3k`, `1.2M` and `1.2G`, here and on the
badge of each reply in the TUI.

`/usage` prints the whole session, one row per model, with the replies of summaries and
sub-agents included:

```text
model             samples   input  cached  written  output   cost
local:qwen              3   36.9k       -        -    2.4k      -
anthropic:sonnet       12  210.0k     91%     4.1k    9.8k  $1.27
total                  15  246.9k     91%     4.1k   12.2k  $1.27
no price, so no cost: local:qwen
```

Cost is worked out when you look, from the prices in your config now, so correcting a price
corrects the whole session. A reply is priced by the model it was sampled with, so a session that
changed models half way adds up right. The sums are exact, not floating point.

When a reply reads nothing from the prompt cache (the server said `cached: 0`) soon after a reply
of the same model that should have written it, τ tells you once per model, with the size of the
prompt that went at full price. It is about money, so only a model with a `price` gets it.
`--set usage.cache_notice=false` silences it, and
`tau ext disable usage` removes all of this.

`tau -p --verbose` prints the same line to stderr (`tau: usage: …`) whenever it changes, as it does
for any extension that shows a line.

## A long conversation can be shortened

Two commands rewrite what the model reads, and both keep the old conversation: they write a new
line in the session's tree and move you onto it. Going back to an old node shows the conversation
as it was (`Ctrl+G` draws the old line as a folded branch).

`/compact [focus]` asks a model to summarize the older part of the conversation and keeps the
newest part word for word. `focus` is text the summary should keep in mind (`/compact the parser
bug`). τ also does this by itself, with two limits. When a finished turn leaves the conversation
past the **soft** limit (80% of the hard one), τ compacts after the agent's last reply, and your
next message starts on the shorter line. If a turn itself grows past the **hard** limit (the
window less `reserve`), τ interrupts it just before the request that would not fit, compacts, and
carries on:

```text
compacted 182k → 23k tokens (nodes 4–418 summarized as node 431)
```

For that τ must know the window, so give the model a `context_window` (as for the `ctx` readout
above). A model without one is never compacted by itself and the status bar says so
(`compaction: local:qwen has no context_window`); `/compact` still works. The settings, with
`/config compaction` or `extensions.compaction` in your config:

```json
{ "extensions": { "compaction": { "reserve": 16384, "keep": 8000 } } }
```

`auto` (`true`) turns the automatic compaction off. `reserve` is the tokens kept free for the reply
and for the summary itself (16384, or a quarter of the window if less); `hard` and `soft` set the two limits directly, as token counts (`soft`
above `hard` is a config error). `keep` the tokens of the newest messages copied unchanged. Left unset,
`keep` is 20000 or half the soft limit if that is less, which suits a 32k
local model; a `keep` you set must be smaller than the soft limit. If the newest messages alone fill the
room, or a summary would not be shorter, nothing is written: at the hard limit the step fails and
says what is too big; at the soft limit τ tells you once and goes on. The soft limit acts only after
a turn that ended with a reply, not when you go back to one. `model` names another model to write the summary
(default: the one you are using). The summary is written by a second cursor, `compaction:1`, which
you can see in the tree and which `/usage` counts; it is closed when it is done. Esc while it
works leaves your conversation exactly as it was. If the summary fails, the step fails and says
why. τ does not compact when the provider answers "prompt too long": run `/compact`, then send
again.

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

Out of the box τ runs whatever tool the model calls and asks nothing, as it always has. Write rules
and it asks. A rule is a tool's name, or a tool's name, a colon and a pattern. There are three
lists, and a call is checked against `deny` first, then `allow`, then `ask`; a call that no rule
names runs:

```json
{
  "extensions": {
    "permissions": {
      "ask": ["bash", "write", "edit"],
      "allow": ["bash:git status*", "bash:git diff*", "bash:ls*"],
      "deny": ["write:/etc/*", "edit:/etc/*"]
    }
  }
}
```

That asks about every `bash`, `write` and `edit` call except the `git status`, `git diff` and `ls`
commands, and refuses outright any write under `/etc`. The one line that turns asking on is
`/config permissions ask=bash,write,edit --save`.

The pattern is a shell-style glob over the whole command (for `bash`) or the `path` argument (for
`read`, `write`, `edit`, `grep` and `find`): `*` matches anything, including `/` and line breaks,
`?` one character, `[ab]` a set, and capitals are not lowercase. A tool an extension gives the model
takes only its bare name (and a name may hold a colon: `srv:tool`). A rule that names a tool nobody
provides (`Bash`, `wirte`) stops the start, so a typo cannot silently guard nothing; `/config
permissions ...` checks the names the same way, and a bad rule is never written, `--save` or not.
A tool of a built-in extension that is off (`agent`, until you enable `agents`) is known, so a
`deny` for it is no error.

Paths are matched as the model wrote them and as the file they name: relative to the session's
directory, with `..`, `//` and symlinks resolved. So `write:/etc/*` also refuses `../../etc/x` and a
link that lands in `/etc`. A rule that starts with a plain relative name (`read:src/*`,
`write:./out/*`) starts in the session's working directory, as the tool's own paths do, so
`allow: ["read:src/*"]` allows `src/a.py` and nothing outside that directory. (`tau config check`
reads the rule as written and has no working directory to resolve it against, so it says nothing
about it.) A rule that starts with `/` or a wildcard is used as it is. `grep` also names each of its
`files` (and the working directory when it names no path): a `deny` or `ask` fires if any of them
matches, an `allow` needs all of them to, and an `allow` matches the resolved path only.

`grep` and `find` read everything under their root, so a `deny` or `ask` for `grep:/etc/*` also
refuses a search that is rooted at `/etc`, at a directory that holds it (`path=/`) or at a link to
it. It refuses such a broad search whole; it does not filter the results. A rule that starts with a
wildcard (`grep:*secret*`) has no directory to compare, and holds only for the paths a search names.

Commands are harder, because a glob is not a shell. An `allow` pattern never runs a command that
chains or redirects (`;`, `&`, `|`, a backtick, `$(`, `<`, `>`, or a new line) unless the pattern
contains that character too: `allow: ["bash:git status*"]` runs `git status -s` but asks about
`git status; rm -rf x`. A `deny` or `ask` pattern is tried on each piece of a chained command, after
dropping leading `VAR=value` words, `if`/`elif`/`while`/`until`/`then`/`do`/`else`, `function NAME`,
a leading backslash and wrappers such as `sudo -u X` (or `--user X`), `env -i`, `nice -n 5`,
`timeout 5`, `doas`, `ionice`, `stdbuf`, `setsid`, `strace`, `watch`, `xargs`, `exec` (with `-a NAME`), `builtin`, `coproc` and `command`, and
reducing `/bin/rm` to `rm`, so `bash:rm *` refuses `ls; rm x`, `if rm x; then ...`, `ls | xargs rm` (a bare
`xargs rm` gets arguments, so it counts as `rm ` with something after it) and `/bin/rm x`. That is best effort: quoting, `eval`, `bash -c "..."` and what `find -exec` runs
are not seen, so **a `bash` deny is not a guarantee. `ask: ["bash"]` is the
safe setting**: it puts every command in front of you. Rules guard against the model; they are not a
sandbox.

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

`tau -p` has no one to ask, so a question it has no answer for is declined and the call refused,
and the run still ends (`tau: declined question 0 from permissions on cursor 0: ...`). Give it answers with `--answer`, which matches who asked and what:

```bash skip
tau -p --answer permissions=yes "tidy the imports"
tau -p --answer 'permissions:bash: rm*=no' --answer permissions=yes "clean up"
```

A `--answer` text is a glob over the call (`bash: git push origin master`; a file tool's question
shows the resolved path, so a text for `/proj/*` does not answer `/proj/../etc/x`), so
`--answer 'permissions:bash: git push*=yes'` is complete as written. It may also be written as a
rule, with no space: `--answer 'permissions:bash:git push*=yes'`. Like an `allow` pattern it
never answers a `bash` command that chains or redirects unless the text contains that character;
other tools' arguments are no shell command, so a `;` in them means nothing. Leave the
text out (`--answer permissions=yes`) to answer every question. An empty pattern (`bash:`) or an
unknown tool name stops the start, and `tau config check` names it.

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
provides stops the start, and `tau config check` reports it. A list typed on a command line is
split at commas and each item trimmed, so `tools=read, grep` is `["read", "grep"]`.
