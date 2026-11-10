"""`scripts/tokens_goldens.py generate`: the reference values, from the reference packages.

Imported only by that command, because it imports `transformers`, `tokenizers`, `tiktoken` and
`jinja2`, which the project's environment does not have (see tokens_goldens.py). Everything here
asks a reference implementation what it prints and writes that down; nothing here is compared
with tau_tokens, which the tests do.

Written under tests/fixtures/tokens/:

    texts.json       the texts every tokenizer counts (made here, by rule, so it is reproducible)
    requests.json    conversations.json's requests, each part as the JSON text a caller passes
    goldens.json     per tokenizer and text: the count and a hash of the ids; per chat template
                     and request: a hash of the rendered text, and its count and ids
    templates.json   the whole rendered text of every templates/*.jinja on every request
    tiny/*.json      four small tokenizers, one per kind of pipeline, and tiny/cases.json
"""

import hashlib
import json
import platform
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import jinja2
import tiktoken
import tokenizers
import transformers
import transformers.utils.chat_template_utils as chat_template_utils
from tokenizers import AddedToken, Regex, Tokenizer, models, normalizers, pre_tokenizers, trainers
from tokens_goldens import ARTIFACTS, FIXTURES, artifact_dir
from transformers import AutoTokenizer

# The time every `strftime_now` reads while the references are made; the tests pass the same.
NOW = (2026, 11, 10, 9, 30, 0)
TOKEN_NAMES = ["bos_token", "eos_token", "unk_token", "sep_token", "pad_token", "cls_token"]
TOKEN_NAMES.append("mask_token")
ENCODINGS = ["o200k_base", "cl100k_base"]
TINY_VOCAB = 700
LLAMA3_SPLIT = (
    r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}{1,3}"
    r"| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+"
)


class FixedClock(datetime):
    """`datetime`, with `now()` pinned: what transformers' `strftime_now` formats."""

    @classmethod
    def now(cls, tz: Any = None) -> "FixedClock":
        return cls(*NOW)


def ids_hash(ids: list[int]) -> str:
    """The hash the tests compare ids by: sha256 of the decimal ids joined by commas."""
    return hashlib.sha256(",".join(map(str, ids)).encode()).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- the texts ----------------------------------------------------------------------------


def sampled(start: int, stop: int, step: int) -> str:
    """Every `step`th code point of a range, surrogates left out, space-separated in runs."""
    points = [chr(c) for c in range(start, stop, step) if not 0xD800 <= c <= 0xDFFF]
    return " ".join("".join(points[i : i + 7]) for i in range(0, len(points), 7))


def whitespace_text() -> str:
    spaces = "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680\u180e\u2000\u2001\u2002\u2003\u2004"
    spaces += "\u2005\u2006\u2007\u2008\u2009\u200a\u200b\u2028\u2029\u202f\u205f\u3000\ufeff"
    parts = [f"a{c}b{c}{c}c {c} d{c}\n{c}e" for c in spaces]
    runs = ["x" + " " * n + "y" for n in range(1, 34)] + ["x" + "\n" * n + "y" for n in range(1, 9)]
    runs += ["x" + "\t" * n + "y" for n in range(1, 6)] + ["a \n b", "a\n \nb", "a\r\n\r\nb", "a \r\n"]
    runs += ["trailing   ", "   leading", " \n ", "\n\n\n", "    def f():\n        return 1\n"]
    return "|".join(parts) + "\n" + "|".join(runs)


def hand_texts() -> dict[str, str]:
    return {
        "empty": "",
        "space": " ",
        "newline": "\n",
        "word": "hello",
        "prose": (
            "The quick brown fox jumps over the lazy dog. It wasn't the first time; she'd seen it "
            "happen before, and they'll see it again. \"Really?\" he asked — 'Really,' she said.\n\n"
            "Tokenizers split text into pieces: words, parts of words, punctuation and spaces. "
            "Internationalization, antidisestablishmentarianism and pneumonoultramicroscopic are "
            "long words; I, a and o are short ones."
        ),
        "contractions": (
            "I'm you're he's it'll we'd they've don't can't won't shouldn't DON'T I'M YOU'RE "
            "IT'LL 'tis 'twas rock'n'roll o'clock can’t it’s we’ll James's dogs' x's 's 't 're "
            "'ve 'm 'll 'd 'S 'T 'RE 'VE 'M 'LL 'D"
        ),
        "python": (
            "import os\nfrom typing import Any\n\n\nclass Counter:\n    \"\"\"Counts things.\"\"\"\n\n"
            "    def __init__(self, start: int = 0) -> None:\n        self._n = start\n\n"
            "    def add(self, *items: Any, **kw: Any) -> int:\n        for i, item in "
            "enumerate(items):\n            if item is None or not item:\n                continue\n"
            "            self._n += 1 if i % 2 == 0 else 2\n        return self._n  # total\n\n\n"
            "if __name__ == \"__main__\":\n    print(f\"{Counter().add(1, 2, 3):>8.2f}\", "
            "os.environ.get('HOME', '~'))\n"
        ),
        "rust": (
            "#[derive(Debug, Clone)]\npub struct Piece<'a> {\n    text: &'a str,\n    first: bool,\n}\n\n"
            "impl<'a> Piece<'a> {\n    pub fn len(&self) -> usize {\n        self.text.chars()"
            ".filter(|c| !c.is_whitespace()).count()\n    }\n}\n\nfn main() -> Result<(), Box<dyn "
            "std::error::Error>> {\n    let v: Vec<u32> = (1..=10).map(|x| x * x).collect();\n"
            "    println!(\"{v:?} {:#x} {:>5.1}\", 255u8, 2.5f64);\n    Ok(())\n}\n"
        ),
        "json": json.dumps(
            {"id": 17, "tags": ["a", "b"], "ratio": 0.25, "nested": {"ok": True, "none": None},
             "text": "line\nbreak \"quoted\" \\ slash", "unicode": "café 日本"},
            indent=2,
        ) + "\n" + json.dumps([{"k": i, "v": [i, i * 1.5, str(i)]} for i in range(6)]),
        "markdown": (
            "# Title\n\n## Sub-title\n\n- item one\n- item **two** with `code` and [a link]"
            "(https://example.com/path?q=1&r=2#frag)\n  - nested item\n1. first\n2. second\n\n"
            "| name | value |\n|------|------:|\n| a    |     1 |\n| bb   |    22 |\n\n> quoted\n\n"
            "```\nfenced\n```\n\n---\n\n<details><summary>More</summary>&amp; &lt;html&gt;</details>\n"
        ),
        "numbers": (
            "0 1 12 123 1234 12345 123456 1234567 12345678 123456789 1234567890 "
            "12345678901234567890 3.14159 -2.5e-10 1,000,000.00 0xDEADBEEF 0b1010 1e100 50% $9.99 "
            "2026-11-10T09:30:00Z 192.168.1.100:8100 +1 (555) 010-9999 ١٢٣٤٥ ۱۲۳ १२३ ² ³ ¹ ½ ¾ "
            "Ⅻ ⅷ ① ⑳ ㊿ 一二三 ０１２３ 𝟘𝟙𝟚"
        ),
        "punctuation": (
            "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~ ... --- === ### *** ___ !!! ??? ;;; ::: ,,, "
            "------------------------------ ============================== “curly” ‘single’ "
            "«guillemets» „low“ … — – ‐ • · § ¶ † ‡ ° ± × ÷ ≠ ≤ ≥ ∞ → ← ↑ ↓ ✓ ✗ ©®™ ¿¡ a.b,c;d:e "
            "(a)[b]{c}<d> a/b\\c|d a-b_c+d=e x.y.z e.g. i.e. U.S.A. end.Start"
        ),
        "scripts": (
            "English Ελληνικά Русский Հայերեն עברית العربية فارسی हिन्दी বাংলা தமிழ் తెలుగు ไทย ລາວ "
            "မြန်မာ ქართული 한국어 日本語 ひらがな カタカナ 中文 繁體字 Tiếng Việt Čeština Türkçe "
            "İstanbul ıi straße ǅ ﬁ ﬂ Å Å é é ñ ñ"
        ),
        "emoji": (
            "😀 😃😄 👍🏽 👩‍👩‍👧‍👦 🇫🇷🇯🇵 🏳️‍🌈 ❤️ ❤ ☺️ ☺ 1️⃣ #️⃣ 🧑🏿‍💻 text😀text 😀\n😀 🫠 🪿 "
            "\U0001fae8 ¯\\_(ツ)_/¯ (╯°□°)╯︵ ┻━┻"
        ),
        "combining": (
            "e\u0301 a\u0308\u0304 o\u0302\u0301 \u0301start Z\u0351\u036b\u0343a\u0328\u0301l\u0321g\u0338o "
            "क्षि ज्ञ ก็ ทำ 각 각 \u200dzwj\u200d \u200czwnj\u200c \u2060wj\u2060 "
            "a\ufe0fb a\u034fb \u202eRTL override\u202c \u2066isolate\u2069"
        ),
        "specials": (
            "<|im_start|>system\nYou are<|im_end|>\n<|im_start|>user <|endoftext|> <s> </s> <unk> "
            "<pad> [INST] [/INST] [TOOL_CALLS] <|begin_of_text|><|start_header_id|>user"
            "<|end_header_id|>\n\n<|eot_id|> <bos> <eos> <start_of_turn>model\n<end_of_turn> "
            "<think>\nhm\n</think> <tool_call> </tool_call> <|start|>assistant<|channel|>final"
            "<|message|>hi<|end|><|return|> <｜begin▁of▁sentence｜><｜User｜>x<｜Assistant｜> "
            "<|system|> <|user|> <|assistant|> <|end|> [gMASK]<sop> <|observation|> "
            "<|endofprompt|> < |im_start| > <|im_start |> <<|im_end|>> <|IM_START|>"
        ),
        "controls": (
            "nul\x00 soh\x01 bel\x07 bs\x08 esc\x1b us\x1f del\x7f pad\x80 nel\x85 apc\x9f "
            "shy\xad bom\ufeff repl\ufffd pua\ue000\uf8ff non\ufffe\uffff sup\U000f0000 "
            "unassigned\u0378\u0530 \U000e0001tag\U000e007f ls\u2028ps\u2029 end"
        ),
        "long-word": "a" * 300 + " " + "ab" * 150 + " " + "Aa1" * 100 + " " + "é" * 120,
        "encoded": (
            "https://user:pass@sub.example.co.uk:8443/a/b%20c/d.html?x=1&y=two#top "
            "mailto:first.last+tag@example.org /usr/local/lib/python3.12/site-packages "
            "C:\\Users\\Name\\file.txt SGVsbG8sIHdvcmxkISBUaGlzIGlzIGJhc2U2NC4= "
            "d41d8cd98f00b204e9800998ecf8427e 550e8400-e29b-41d4-a716-446655440000 "
            "snake_case_name camelCaseName PascalCaseName SCREAMING_CASE kebab-case-name"
        ),
    }


def corpus() -> dict[str, str]:
    """The texts every tokenizer counts, by name."""
    texts = hand_texts()
    texts["whitespace"] = whitespace_text()
    texts["bmp-sample"] = sampled(0x20, 0x10000, 97)
    texts["astral-sample"] = sampled(0x10000, 0x30000, 1009)
    texts["latin-all"] = "".join(chr(c) for c in range(0x20, 0x250))
    return texts


# ---- tiktoken -----------------------------------------------------------------------------


def counted(ids: list[int]) -> dict[str, Any]:
    return {"count": len(ids), "ids": ids_hash(ids)}


def tiktoken_goldens(texts: dict[str, str]) -> dict[str, Any]:
    out = {}
    for name in ENCODINGS:
        encoding = tiktoken.get_encoding(name)
        counts = {key: counted(encoding.encode_ordinary(text)) for key, text in texts.items()}
        out[name] = {"vocab": encoding.n_vocab, "texts": counts}
    return out


# ---- the tiny tokenizers ------------------------------------------------------------------


def special(text: str, **how: bool) -> AddedToken:
    return AddedToken(text, special=True, normalized=False, **how)


def tiny_bytelevel(training: list[str]) -> Tokenizer:
    """Byte-level BPE with special and plain added tokens: the shape of Qwen's tokenizer."""
    tokenizer = Tokenizer(models.BPE())
    tokenizer.normalizer = normalizers.NFC()
    tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    trainer = trainers.BpeTrainer(
        vocab_size=TINY_VOCAB, initial_alphabet=pre_tokenizers.ByteLevel.alphabet()
    )
    tokenizer.train_from_iterator(training, trainer)
    tokenizer.add_special_tokens([special("<|im_start|>"), special("<|im_end|>")])
    tokenizer.add_special_tokens([special("<|rs|>", rstrip=True), special("<|ls|>", lstrip=True)])
    tokenizer.add_tokens([AddedToken("<think>", normalized=False), AddedToken("</think>", normalized=False)])
    tokenizer.add_tokens([AddedToken("WORD", single_word=True, normalized=False)])
    return tokenizer


def tiny_metaspace(training: list[str]) -> Tokenizer:
    """BPE over Metaspace that marks only the first word of the text, with a normalizer: the
    one pipeline where a piece tokenizes differently at the start of the text."""
    tokenizer = Tokenizer(models.BPE(unk_token="<unk>"))
    tokenizer.normalizer = normalizers.Sequence([normalizers.NFKC(), normalizers.Replace("``", '"')])
    tokenizer.pre_tokenizer = pre_tokenizers.Metaspace(replacement="▁", prepend_scheme="first")
    trainer = trainers.BpeTrainer(vocab_size=TINY_VOCAB, special_tokens=["<unk>", "<s>", "</s>"])
    tokenizer.train_from_iterator(training, trainer)
    tokenizer.add_special_tokens([special("[INST]"), special("[/INST]")])
    return tokenizer


def tiny_wordpiece(training: list[str]) -> Tokenizer:
    """WordPiece under a lowercasing normalizer, with an added token that is matched after
    normalization (`normalized=True`)."""
    tokenizer = Tokenizer(models.WordPiece(unk_token="[UNK]"))
    tokenizer.normalizer = normalizers.BertNormalizer(lowercase=True)
    tokenizer.pre_tokenizer = pre_tokenizers.BertPreTokenizer()
    trainer = trainers.WordPieceTrainer(
        vocab_size=TINY_VOCAB, special_tokens=["[UNK]", "[CLS]", "[SEP]"]
    )
    tokenizer.train_from_iterator(training, trainer)
    tokenizer.add_tokens([AddedToken("tokenizers", normalized=True), AddedToken("[Tool]", normalized=True)])
    return tokenizer


def tiny_split(training: list[str]) -> Tokenizer:
    """BPE over a regex split with a lookahead (Llama 3's pattern): what the pure-Rust regex
    engine has to agree with Oniguruma on."""
    tokenizer = Tokenizer(models.BPE())
    tokenizer.pre_tokenizer = pre_tokenizers.Sequence(
        [
            pre_tokenizers.Split(Regex(LLAMA3_SPLIT), behavior="isolated"),
            pre_tokenizers.ByteLevel(add_prefix_space=False, use_regex=False),
        ]
    )
    trainer = trainers.BpeTrainer(
        vocab_size=TINY_VOCAB, initial_alphabet=pre_tokenizers.ByteLevel.alphabet()
    )
    tokenizer.train_from_iterator(training, trainer)
    tokenizer.add_special_tokens(
        [special(t) for t in ["<|begin_of_text|>", "<|start_header_id|>", "<|end_header_id|>", "<|eot_id|>"]]
    )
    return tokenizer


TINY = {
    "bytelevel": (tiny_bytelevel, ["<|im_start|>", "<|im_end|>", "<|rs|>", "<|ls|>", "<think>", "</think>", "WORD"]),
    "metaspace": (tiny_metaspace, ["<s>", "</s>", "[INST]", "[/INST]", "<unk>"]),
    "wordpiece": (tiny_wordpiece, ["[CLS]", "[SEP]", "tokenizers", "[Tool]", "[tool]", "TOKENIZERS"]),
    "split": (tiny_split, ["<|begin_of_text|>", "<|start_header_id|>", "<|end_header_id|>", "<|eot_id|>"]),
}


def framed(texts: dict[str, str], marks: list[str]) -> list[str]:
    """Texts with a tokenizer's added tokens in them, the way a rendered request has them: at
    the start, between messages, touching words, next to spaces, doubled."""
    a, b = marks[0], marks[1]
    short = [texts[k] for k in ("word", "prose", "contractions", "python", "scripts", "whitespace")]
    out = [f"{a}user\n{t}{b}\n{a}assistant\n" for t in short]
    out += [f"{t}{a}{t}" for t in short[:3]]
    out += [" ".join(f"{m} {texts['word']} {m}{m}x{m} " for m in marks)]
    out += ["".join(marks), " ".join(marks), "\n".join(reversed(marks)), f"  {a}  {b}  ", f"x{a}"]
    out += [f"{a}{short[1]}{b}" * 3, f"{b}\n{a}".join(short)]
    return out


def tiny_goldens(texts: dict[str, str]) -> dict[str, Any]:
    training = [t for k, t in texts.items() if k not in ("bmp-sample", "astral-sample")] * 3
    cases: dict[str, Any] = {}
    for name, (build, marks) in TINY.items():
        tokenizer = build(training)
        (FIXTURES / "tiny").mkdir(exist_ok=True)
        tokenizer.save(str(FIXTURES / "tiny" / f"{name}.json"))
        every = list(texts.values()) + framed(texts, marks)
        cases[name] = {
            "vocab": tokenizer.get_vocab_size(with_added_tokens=True),
            "marks": marks,
            "cases": [{"text": t, **counted(tokenizer.encode(t, add_special_tokens=False).ids)} for t in every],
        }
    return cases


# ---- the real files -----------------------------------------------------------------------


def template_of(directory: Path) -> Any:
    """A repository's chat template as tau_tokens is given it: chat_template.jinja when there is
    one, else tokenizer_config.json's."""
    jinja = directory / "chat_template.jinja"
    if jinja.exists():
        return jinja.read_text(encoding="utf-8")
    return json.loads((directory / "tokenizer_config.json").read_text(encoding="utf-8"))["chat_template"]


def config_tokens(directory: Path) -> dict[str, str]:
    """The named special tokens of a tokenizer_config.json. Only for a repository whose
    tokenizer.json was not fetched: where it was, transformers itself says what they are."""
    config = json.loads((directory / "tokenizer_config.json").read_text(encoding="utf-8"))
    out = {}
    for name in TOKEN_NAMES:
        value = config.get(name)
        if isinstance(value, dict):
            value = value.get("content")
        if isinstance(value, str):
            out[name] = value
    return out


def request_of(conversation: dict[str, Any], tool_sets: dict[str, Any]) -> dict[str, Any]:
    return {
        "messages": conversation["messages"],
        "tools": tool_sets[conversation["tools"]] if "tools" in conversation else None,
        "add_generation_prompt": conversation.get("generate", True),
        "extra": conversation.get("extra", {}),
    }


def render_reference(template: Any, tokens: dict[str, str], request: dict[str, Any]) -> str:
    """transformers' rendering of a request with a template given as text (or as the named
    templates of a config), as `apply_chat_template` does it."""
    if isinstance(template, list):
        named = {t["name"]: t["template"] for t in template}
        template = named["tool_use"] if request["tools"] is not None and "tool_use" in named else named["default"]
    rendered, _ = chat_template_utils.render_jinja_template(
        conversations=[json.loads(json.dumps(request["messages"]))],
        tools=request["tools"],
        chat_template=template,
        add_generation_prompt=request["add_generation_prompt"],
        **{**tokens, **request["extra"]},
    )
    return rendered[0]


def attempt(render: Any) -> tuple[str | None, str | None]:
    """(rendered, None), or (None, why) when the reference refuses the request. A template may
    refuse (`raise_exception`) or fail on a request it was not written for; both are a result."""
    try:
        return render(), None
    except (jinja2.TemplateError, TypeError, ValueError, KeyError, AttributeError, IndexError) as err:
        return None, f"{type(err).__name__}: {err}"[:200]


def artifact_requests(
    artifact: dict[str, Any], tokenizer: Any, conversations: dict[str, Any], dump: Path | None
) -> dict[str, Any]:
    directory = artifact_dir(artifact)
    template, tokens = template_of(directory), config_tokens(directory)
    out = {}
    for conversation in conversations["conversations"]:
        request = request_of(conversation, conversations["tools"])
        if tokenizer is None:
            rendered, why = attempt(lambda r=request: render_reference(template, tokens, r))
        else:
            rendered, why = attempt(
                lambda r=request: tokenizer.apply_chat_template(
                    json.loads(json.dumps(r["messages"])),
                    tools=r["tools"],
                    add_generation_prompt=r["add_generation_prompt"],
                    tokenize=False,
                    **r["extra"],
                )
            )
        if rendered is None:
            out[conversation["name"]] = {"error": why}
            continue
        golden: dict[str, Any] = {"rendered": text_hash(rendered), "bytes": len(rendered.encode())}
        if tokenizer is not None:
            golden |= counted(tokenizer(rendered, add_special_tokens=False)["input_ids"])
        out[conversation["name"]] = golden
        if dump is not None:
            path = dump / artifact["name"] / f"{conversation['name']}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
    return out


def artifact_goldens(
    artifact: dict[str, Any], texts: dict[str, str], conversations: dict[str, Any], dump: Path | None
) -> dict[str, Any] | None:
    directory = artifact_dir(artifact)
    if not (directory / "tokenizer_config.json").exists():
        print(f"generate: {artifact['name']}: files absent (fetch them); left out", file=sys.stderr)
        return None
    out: dict[str, Any] = {"repo": artifact["repo"], "revision": artifact["revision"]}
    tokenizer = None
    if artifact["count"]:
        # Two references for a text: the `tokenizers` library on tokenizer.json alone, which is
        # what tau_tokens loads, and transformers' tokenizer for the repository, which is what
        # a server loads. They must agree for tokenizer.json alone to be enough.
        raw = Tokenizer.from_file(str(directory / "tokenizer.json"))
        raw.no_truncation()
        raw.no_padding()
        tokenizer = AutoTokenizer.from_pretrained(str(directory))
        out["vocab"] = raw.get_vocab_size(with_added_tokens=True)
        out["texts"] = {}
        for key, text in texts.items():
            ids = raw.encode(text, add_special_tokens=False).ids
            served = tokenizer(text, add_special_tokens=False)["input_ids"]
            out["texts"][key] = counted(ids) | ({} if ids == served else {"transformers": counted(served)})
    out["requests"] = artifact_requests(artifact, tokenizer, conversations, dump)
    return out


def template_goldens(conversations: dict[str, Any]) -> dict[str, Any]:
    """Every templates/*.jinja on every request, whole: the engine against Jinja2 itself."""
    tokens = {"bos_token": "<s>", "eos_token": "</s>"}
    out: dict[str, Any] = {}
    for path in sorted((FIXTURES / "templates").glob("*.jinja")):
        source = path.read_text(encoding="utf-8")
        out[path.stem] = {}
        for conversation in conversations["conversations"]:
            request = request_of(conversation, conversations["tools"])
            rendered, why = attempt(lambda r=request, s=source: render_reference(s, tokens, r))
            out[path.stem][conversation["name"]] = {"error": why} if rendered is None else {"rendered": rendered}
    return out


def request_texts(conversations: dict[str, Any]) -> list[dict[str, Any]]:
    """Each request of conversations.json with its parts as JSON text, keys in the order written:
    what a caller hands tau_tokens. (A test in Rust cannot read conversations.json itself and
    keep that order: serde_json sorts an object's keys.)"""
    out = []
    for conversation in conversations["conversations"]:
        request = request_of(conversation, conversations["tools"])
        text = {k: json.dumps(request[k], ensure_ascii=False) for k in ("messages", "tools", "extra")}
        out.append(
            {
                "name": conversation["name"],
                "messages": text["messages"],
                "tools": None if request["tools"] is None else text["tools"],
                "generate": request["add_generation_prompt"],
                "extra": text["extra"] if request["extra"] else None,
            }
        )
    return out


def write(name: str, data: Any) -> None:
    text = json.dumps(data, indent=1, ensure_ascii=False, sort_keys=False) + "\n"
    (FIXTURES / name).write_text(text, encoding="utf-8")
    print(f"generate: wrote tests/fixtures/tokens/{name} ({len(text.encode())} bytes)")


def generate(dump: Path | None) -> int:
    chat_template_utils.datetime = FixedClock  # what `strftime_now` calls `.now()` on
    texts = corpus()
    conversations = json.loads((FIXTURES / "conversations.json").read_text(encoding="utf-8"))
    manifest = json.loads(ARTIFACTS.read_text(encoding="utf-8"))
    artifacts = {}
    for artifact in manifest["artifacts"]:
        golden = artifact_goldens(artifact, texts, conversations, dump)
        if golden is not None:
            artifacts[artifact["name"]] = golden
    write("texts.json", texts)
    write("requests.json", request_texts(conversations))
    write("templates.json", {"now": list(NOW), "tokens": {"bos_token": "<s>", "eos_token": "</s>"},
                             "templates": template_goldens(conversations)})
    write("tiny/cases.json", tiny_goldens(texts))
    write(
        "goldens.json",
        {
            "reference": {
                "python": platform.python_version(),
                "transformers": transformers.__version__,
                "tokenizers": tokenizers.__version__,
                "tiktoken": tiktoken.__version__,
                "jinja2": jinja2.__version__,
            },
            "now": list(NOW),
            "tiktoken": tiktoken_goldens(texts),
            "artifacts": artifacts,
        },
    )
    return 0
