"""
Bug 6 regression — the recording bundle must ship KEY-FREE.

scripts/freeze_backend.py used to bake the real AWS_BEARER_TOKEN_BEDROCK into
animora_backend.env inside the installer, where it was trivially extractable
(the file's own comment admitted it). That was the only client-embedded
credential left in the product. The env file now carries the NON-SECRET
region only; the operator supplies the key via the environment at runtime.

_recording_env_file_contents() is pure (stdlib only) so this guard runs
without PyInstaller or a build.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_FREEZE = Path(__file__).resolve().parent.parent.parent / "scripts" / "freeze_backend.py"


def _load():
    spec = importlib.util.spec_from_file_location("animora_freeze_backend", _FREEZE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_recording_env_has_no_bedrock_key():
    contents = _load()._recording_env_file_contents()
    # The key line must NOT assign a value. An empty/instructional mention is
    # fine, but there must be no "AWS_BEARER_TOKEN_BEDROCK=<something>".
    for raw in contents.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        assert not line.startswith("AWS_BEARER_TOKEN_BEDROCK="), (
            f"recording bundle env assigns the Bedrock key: {line!r}"
        )
    # No ABSK... token anywhere, comment or not.
    assert "ABSK" not in contents.replace("ABSK...", "")  # the literal example is allowed
    # Still carries the non-secret region so the region isn't left unset.
    assert "BEDROCK_AWS_REGION=" in contents


def test_recording_env_tells_operator_to_set_the_key():
    contents = _load()._recording_env_file_contents()
    assert "AWS_BEARER_TOKEN_BEDROCK" in contents  # mentioned in the instructions
    assert "set " in contents or "export " in contents  # how to provide it
