"""Test-suite defaults.

Imported by pytest before any test module, which is what makes the environment
override below effective: `app.config` reads its settings once, lazily, and
caches them, so the variable has to be set before the first import rather than
inside a fixture.

The vision-language model is forced OFF for the whole suite.

Enabling it in `.env` is a deployment decision, and it silently turned the unit
tests into integration tests against a local Ollama daemon: the run went from
20 seconds to 132, two tests failed for reasons unrelated to what they assert,
and a blank white image came back COMPLETED because the model answered a
question about a page with nothing on it. A test suite that depends on a 4GB
model being installed and running is not a test suite.

Tests that genuinely exercise the VLM path build their own Settings with
`vlm_enabled=True` and stub the transport, which is the only way the assertions
stay about our code rather than about the model's mood.
"""

from __future__ import annotations

import os

# Set before app.config is imported anywhere. Environment variables take
# precedence over .env in pydantic-settings, so this wins over a deployment
# that has the model switched on.
os.environ["VLM_ENABLED"] = "false"
