"""The models a client may know of (D32 section 8): `Client.models()`, one `ModelInfo` each.

A client cannot read the driver's config files, and a `Config` node holds only a qualified name
(D13), so the facts a client shows about a model (its context window for a meter, its price for a
cost, whether it takes images) come from the driver's catalog, through this call. The fields are
an allowlist, written out here: a model's `base_url`, its API key or the name of the variable
holding it, and its provider `options` (headers among them) never leave the driver, and a key added
to the config later is not sent until a field is added here for it.

What is listed is the effective catalog, every layer folded (user, project, `--set`), so it is the
config samples use. A price is the `usage` extension's (D28): its section as its own
`model_options` type reads it, so τ itself never reads a price, and a session without `usage`
lists none. The answer changes only when the driver's catalog is replaced (`Driver.use_catalog`:
a `configure(..., save=True)` reads the files again) or a reload changes `usage`; a change in what
this call would answer sends `Commands` (D32 section 3), on which a client calls `models()` again.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from tau._config import ConfigError, ModelConfig
from tau._config_layers import ModelCatalog
from tau._images import ImageLimits
from tau._protocols import Provider

PRICED_BY = "usage"
"""The extension whose model section holds a model's `price` (D28 section 7), read as that
extension's own `model_options` type reads it."""


@dataclass(frozen=True)
class ModelPrice:
    """A model's price in USD per million tokens, each an exact decimal number as text (`"3"`,
    `"0.3"`, `"3.75"`; never a float, never an exponent): `input` (prompt tokens neither read from
    nor written to the cache), `output`, `cache_read`, `cache_write`, and `cache_write_1h` (a
    cache write kept for an hour; `cache_write` when the config gives none)."""

    input: str
    output: str
    cache_read: str
    cache_write: str
    cache_write_1h: str


@dataclass(frozen=True)
class ModelInfo:
    """What a client may know of one model in the driver's catalog (D32 section 8): its qualified
    `name` (what a `Config` node and `CursorView.model` hold), its `provider`, `context_window`
    and `max_tokens` (the most output tokens a reply asks for; `None`: the provider's default),
    `images` (what one image may be, when the model takes images and its provider sends them;
    `None`: an image for it is refused), `price` (`None`: the model has none), and whether it is
    the catalog's `default_model` (`default`). Nothing else of the config is in it."""

    name: str
    provider: str
    context_window: int | None
    max_tokens: int | None
    images: ImageLimits | None
    price: ModelPrice | None
    default: bool


def _exact(amount: Decimal) -> str:
    """`amount` as plain decimal text with no exponent and no trailing zeros: `3`, `0.3`."""
    text = format(amount, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


_AMOUNTS = ("input", "output", "cache_read", "cache_write")


def _price(name: str, section: object | None) -> ModelPrice | None:
    """`name`'s price as the `usage` extension read its section (`section`, its `model_options`;
    `None`: no loaded extension named `usage` declares one): its `price`, four `Decimal` amounts
    and an optional `cache_write_1h`. τ never reads the section itself (D28 sections 6 and 7:
    usage is a leaf). `ConfigError` naming the model for a `price` of another shape (an extension
    in usage's place that prices some other way)."""
    price: object = getattr(section, "price", None)
    if price is None:
        return None
    four: list[object] = [getattr(price, k, None) for k in _AMOUNTS]
    hour: object = getattr(price, "cache_write_1h", None)
    hour = four[3] if hour is None else hour
    if not all(isinstance(a, Decimal) for a in (*four, hour)):
        raise ConfigError(
            f"model {name!r}: the {PRICED_BY} extension's price is not {list(_AMOUNTS)} "
            "(and cache_write_1h) as Decimal amounts"
        )
    i, o, r, w, h = (_exact(cast("Decimal", a)) for a in (*four, hour))
    return ModelPrice(i, o, r, w, h)


def _images(config: ModelConfig, provider: Provider | None) -> ImageLimits | None:
    """The limits one image is held to on this model, or `None` if it takes none (the image
    rule's, D30 section 6: `images: true`, on a provider that declares `image_limits`)."""
    if not config.images:
        return None
    limits: object = getattr(provider, "image_limits", None)
    return limits if isinstance(limits, ImageLimits) else None


def model_list(
    catalog: ModelCatalog,
    providers: Mapping[str, Provider],
    priced: Callable[[str], object | None],
) -> tuple[ModelInfo, ...]:
    """Every model of `catalog`, in file order, as a client may know it (the module docstring).
    `priced(name)` is the `usage` extension's section of the model `name` as its `model_options`
    type builds it, or `None` when no loaded extension of that name declares one (no price is then
    listed: nothing in this session prices a reply). `ConfigError` from it, or for a `price` of
    another shape."""
    found: list[ModelInfo] = []
    for name in catalog.names():
        config = catalog.resolve(name)
        found.append(
            ModelInfo(
                name,
                config.provider,
                config.context_window,
                config.max_tokens,
                _images(config, providers.get(config.provider)),
                _price(name, priced(name)),
                name == catalog.default_model,
            )
        )
    return tuple(found)


def listed_from(catalog: ModelCatalog) -> object:
    """What `model_list` reads of `catalog`, comparable: two catalogs equal in it list the same
    models, given the same extensions (the providers are the driver's for its life). The hub
    compares it, so a `Commands` is sent when the models change, and not when only a key, a URL or
    an option did."""
    return (
        catalog.default_model,
        tuple(
            (
                name,
                config.provider,
                config.context_window,
                config.max_tokens,
                config.images,
                config.sections.get(PRICED_BY),
            )
            for name, config in ((n, catalog.resolve(n)) for n in catalog.names())
        ),
    )


def _window(tokens: int) -> str:
    """A context window as a list shows it: `200k`, `1M`, `8192`."""
    if tokens >= 1_000_000:  # noqa: PLR2004 - the unit
        return f"{tokens / 1_000_000:.3g}M"
    if tokens >= 1000:  # noqa: PLR2004 - the unit
        return f"{round(tokens / 1000)}k"
    return str(tokens)


def model_detail(model: ModelInfo) -> str:
    """`model` on one short line, as a picker shows it beside the name: the provider, the context
    window, `images` if it takes them, and the input and output price per million tokens:
    `anthropic · 200k ctx · images · $3/$15`. What is not known is left out."""
    said = [model.provider]
    if model.context_window is not None:
        said.append(f"{_window(model.context_window)} ctx")
    if model.images is not None:
        said.append("images")
    if model.price is not None:
        said.append(f"${model.price.input}/${model.price.output}")
    return " · ".join(said)
