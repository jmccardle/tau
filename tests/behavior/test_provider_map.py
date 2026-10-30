"""The driver's map of provider name to provider (D15)."""

from pathlib import Path

import pytest
from fakes import FAKE_CONFIG, FAKE_MODEL, FakeClock, FakeProvider, Reply, fake_catalog
from harness import Harness, scenario

from tau_next import Body, Driver, Part, Session, read_models


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
    assert "provider 'acme'" in errors[0]
    assert "['fake']" in errors[0]
    assert provider.requests == [], "nothing was sampled"
    await h.close()


def test_a_provider_must_be_given_under_its_own_name() -> None:
    session = Session.create(FAKE_CONFIG, "sys", FakeClock().now())
    with pytest.raises(ValueError, match="'fake' is given as 'other'"):
        Driver(session, {"other": FakeProvider([])}, fake_catalog())
    session.close()
