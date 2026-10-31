"""The driver's map of provider name to provider (D15)."""

from pathlib import Path

import pytest
from fakes import (
    FAKE_CONFIG,
    FAKE_MODEL,
    FAKE_PROVIDER,
    FakeClock,
    FakeProvider,
    Reply,
    fake_catalog,
)
from harness import Harness, scenario

from tau import Body, Driver, Part, Session, read_models


@scenario
async def test_a_model_naming_a_provider_the_driver_lacks_fails_that_sample(
    tmp_path: Path,
) -> None:
    catalog = read_models(cli={"models": {FAKE_MODEL: {"model": "m", "provider": "acme"}}})
    provider = FakeProvider([Reply([Part.Text("hi")])])
    h = await Harness.create(tmp_path, provider=provider, catalog=catalog)
    await h.send("hello")
    await h.settle()
    bodies = [n.body for n in h.nodes()]
    errors = [b.data for b in bodies if isinstance(b, Body.Custom) and b.kind == "sample_error"]
    assert len(errors) == 1
    assert "acme" in errors[0], "the provider the model asked for"
    assert FAKE_PROVIDER in errors[0], "the providers the driver has"
    assert provider.requests == [], "nothing was sampled"
    await h.close()


@scenario
async def test_the_providers_problems_with_a_model_fail_the_sample_before_it_starts(
    tmp_path: Path,
) -> None:
    problems = ["unknown option 'seed'", "needs an API key"]
    provider = FakeProvider([Reply([Part.Text("hi")]), Reply([Part.Text("again")])], problems)
    h = await Harness.create(tmp_path, provider=provider)
    await h.send("hello")
    await h.settle()
    h.check()
    errors = [n.body.data for n in h.nodes() if isinstance(n.body, Body.Custom)]
    assert len(errors) == 1
    assert all(p in errors[0] for p in problems), "every problem, not the first"
    assert FAKE_MODEL in errors[0], "named for the model"
    assert provider.requests == [], "nothing was sampled"
    assert not h.session.cursor(h.cursor).auto, "a failed step turns auto-step off"
    assert not any(isinstance(b, Body.Assistant) for b in h.bodies())
    await h.close()


def test_a_provider_must_be_given_under_its_own_name() -> None:
    session = Session.create(FAKE_CONFIG, "sys", FakeClock().now())
    with pytest.raises(ValueError, match="'fake' is given as 'other'"):
        Driver(session, {"other": FakeProvider([])}, fake_catalog())
    session.close()
