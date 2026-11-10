# ffwf-tau-tokens

Local tokenizers for [τ (tau)](https://github.com/jmccardle/tau), an agent harness. This wheel is
what `pip install 'ffwf-tau[tokens]'` adds: it lets τ count a request's tokens before sending it,
with the model's own tokenizer and chat template, instead of estimating.

It is of little use alone, and it depends on nothing. Install it through the extra, which pins it
to the version of `ffwf-tau` it was built with:

```bash
pip install 'ffwf-tau[tokens]'
```

## What is in it

The package is `tau_tokens`. Bytes in, counts out: it reads no file, opens no connection and reads
no clock.

- `Tokenizer`: OpenAI's `o200k_base` and `cl100k_base` (embedded), or any Hugging Face
  `tokenizer.json` given as bytes. `encode(text)` and `count(text)`; a count remembers the pieces
  of text it has seen, so counting a growing conversation again is cheap.
- `ChatTemplate`: a model's chat template, rendered as `transformers`' `apply_chat_template`
  renders it, from the template's text or a `tokenizer_config.json`.
- `parse_identity`, `catalogue`, `candidates`, `verdict`: the names of tokenizers
  (`tiktoken:o200k_base`, `hf:OWNER/REPO@REVISION`, `file:/abs/tokenizer.json`), a catalogue of
  the common ones, and a way to tell from a server's reported counts which one it uses.

```python
from tau_tokens import ChatTemplate, Tokenizer

tokenizer = Tokenizer.from_json(open("tokenizer.json", "rb").read())
template = ChatTemplate.from_config(open("tokenizer_config.json", "rb").read())
text = template.render([{"role": "user", "content": "Hello!"}])
print(tokenizer.count(text))
```

Counts and renderings are tested against the reference implementations (`tokenizers`, `tiktoken`,
`transformers`) for Qwen3, Llama 3, Mistral (Tekken), Gemma 3 and 4, gpt-oss, DeepSeek, Phi-4-mini
and GLM. Whether a given server bills the same count is for its own reports to confirm, which is
what τ does before it calls a count exact.

The [reference](https://github.com/jmccardle/tau/blob/master/docs/reference/tokenizers.md) lists
every name and signature. MIT licensed.
