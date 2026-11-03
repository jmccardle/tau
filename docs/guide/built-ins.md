# What τ does for you

Three things happen in a session without any extension of yours: a failed request is tried again,
`@file` in a message attaches the file, and files you write become commands (prompt templates and
skills). Each can be switched off, and each is a few lines to use. The examples run with the mock
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
off; it skips only installed packages and drop-ins.

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
`see @notes.txt.` attaches `notes.txt`. A word that names no regular file stays as you typed it
(`@alice`, a missing file, a directory), and a file named twice is attached once.

A file over 10 kB, a file that is not UTF-8 and a file that cannot be read are not pasted. The
model is given a reference instead, so it can `read` the part it needs:

```text
<reference path="/work/big.log" size="2.1 MB" reason="over 10 kB"/>
```

The limit is an option, in bytes. `--set attachments.limit=100000` changes it for one run,
`/config attachments limit=100000 --save` for good. `tau ext disable attachments` turns `@file` off.
The terminal UI completes the path when you press Tab after an `@` word.

Every message is expanded the same way, whoever wrote it: the ones you type, the ones a prompt
template sends, the task an extension gives another cursor.

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

as your message, and the model answers it. `$ARGUMENTS` is everything after the command, as typed,
and `$1` to `$9` are its words, split as a shell splits them, so quotes group. A `$N` the
arguments do not have fails the command and sends nothing. A template with no `$` placeholder gets
the arguments after a blank line, which makes `Be brief.` a template you can follow with any
question. The command's help is the `description`; without one it is the first line of the file.
`argument-hint` is what the terminal UI's form shows for the arguments.

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
then `report.pdf`. A skill also reaches the model by itself: at the first step of a new session,
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

A name is letters, digits, `_` and `-`; a template and a skill with one name in one directory, a
bad name, a file that is not UTF-8 and a skill without a description each stop the start with a
message naming the file, because a command that quietly went missing would be harder to find.
`tau ext disable skills` starts without them.

A file may begin with frontmatter, `key: value` lines between two `---` lines. Read are
`description` (one line), `argument-hint`, `disable-model-invocation` (`true` or `false`) and
`name` (which, if given, must be the file's name). Other keys, comments and indented lines are
ignored, so a Claude Code skill loads as it is. There is no YAML: `description: >` is refused,
not misread.

A project's files are text the model will be told to follow, so they are pinned like the rest of
`.tau/`: `tau trust` lists them, a project is asked about again when one is added, removed or
changed, and τ reads the bytes that were approved. Because they are pinned, a τ that predates this
asks once more about every project it trusted before ([Configuration](configuration.md#projects-and-trust)).

Templates and skills are read once, when τ starts. Edit one, then start a new `tau` to use it.
