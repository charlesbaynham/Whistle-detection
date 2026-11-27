from pathlib import Path

import pytest

OURS = str(Path(__file__).parents[1] / "custom_components")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(hass, enable_custom_integrations):
    # The hass fixture's config dir brings its own custom_components package; add ours to it.
    import custom_components

    if OURS not in custom_components.__path__:
        custom_components.__path__.append(OURS)
    yield
