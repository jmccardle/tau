"""Real tokenizers and chat templates against the reference implementations: Qwen3, Llama 3,
Tekken, Gemma 3 and 4, gpt-oss, DeepSeek, Phi-4-mini and GLM count; eight more repositories
render. The files are fetched (`real_files`, conftest.py); the references are goldens.json.

For every text: the ids `tokenizers` gives for the same `tokenizer.json`, which are also the ids
`transformers` gives for the repository. For every request: the text `apply_chat_template`
renders, and the ids of that text. Where the reference refuses a request, so must `tau_tokens`."""

from datetime import datetime
from pathlib import Path
from typing import Any

from tokens_golden import ids_hash, load, text_hash

from tau_tokens import ChatTemplate, TemplateError, Tokenizer, catalogue

ARTIFACTS = load("artifacts.json")["artifacts"]
COUNTED = [a["name"] for a in ARTIFACTS if a["count"]]


def template_of(directory: Path) -> ChatTemplate:
    """A repository's template as a caller gets it: chat_template.jinja when there is one."""
    jinja = directory / "chat_template.jinja"
    source = jinja.read_text(encoding="utf-8") if jinja.exists() else None
    return ChatTemplate.from_config((directory / "tokenizer_config.json").read_bytes(), source)


def test_every_text_has_the_reference_ids(
    real_files: dict[str, Path], goldens: Any, texts: dict[str, str]
) -> None:
    assert len(COUNTED) == 9
    differing = []
    for name in COUNTED:
        tokenizer = Tokenizer.from_json((real_files[name] / "tokenizer.json").read_bytes())
        golden = goldens["artifacts"][name]["texts"]
        for key, text in texts.items():
            assert "transformers" not in golden[key], "the two references agree on every text"
            ids = tokenizer.encode(text)
            if ids_hash(ids) != golden[key]["ids"] or tokenizer.count(text) != golden[key]["count"]:
                differing.append((name, key, len(ids), golden[key]["count"]))
    assert differing == []


def test_every_request_renders_and_counts_as_the_reference(
    real_files: dict[str, Path], goldens: Any, requests: list[dict[str, Any]], now: datetime
) -> None:
    differing: list[tuple[str, str, str]] = []
    rendered_equal = refused_by_both = 0
    for artifact in ARTIFACTS:
        name = artifact["name"]
        template = template_of(real_files[name])
        tokenizer = None
        if artifact["count"]:
            tokenizer = Tokenizer.from_json((real_files[name] / "tokenizer.json").read_bytes())
        for request in requests:
            golden = goldens["artifacts"][name]["requests"][request["name"]]
            try:
                text = template.render(
                    request["messages"],
                    request["tools"],
                    add_generation_prompt=request["add_generation_prompt"],
                    extra=request["extra"],
                    now=now,
                )
            except TemplateError as err:
                refused_by_both += "error" in golden
                if "error" not in golden:
                    differing.append((name, request["name"], f"we refuse: {err}"))
                continue
            if golden.get("rendered") != text_hash(text):
                differing.append((name, request["name"], "the rendering differs"))
                continue
            rendered_equal += 1
            if tokenizer is not None:
                ids = tokenizer.encode(text)
                if ids_hash(ids) != golden["ids"] or tokenizer.count(text) != golden["count"]:
                    differing.append(
                        (name, request["name"], f"{len(ids)} ids, not {golden['count']}")
                    )
    assert differing == []
    assert rendered_equal + refused_by_both == len(ARTIFACTS) * len(requests) == 340
    assert rendered_equal >= 290, "most requests are rendered, not refused"


def test_the_catalogue_states_the_sizes_the_files_have(
    real_files: dict[str, Path], goldens: Any
) -> None:
    listed = {t["id"]: t for t in catalogue()}
    for artifact in ARTIFACTS:
        if not artifact["count"]:
            continue
        tokenizer = listed[f"hf:{artifact['repo']}@{artifact['revision']}"]
        assert tokenizer["vocab"] == goldens["artifacts"][artifact["name"]]["vocab"]
        data = (real_files[artifact["name"]] / "tokenizer.json").read_bytes()
        assert Tokenizer.from_json(data).vocab_size == tokenizer["vocab"]
    assert sum(t["id"].startswith("hf:") for t in catalogue()) == len(COUNTED)
