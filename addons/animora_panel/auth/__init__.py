"""Animora desktop authentication.

Loopback-callback PKCE sign-in (RFC 8252 §7.3) against Supabase:

  1. controller.begin_sign_in() generates PKCE + state, starts a one-shot
     loopback HTTP listener on 127.0.0.1, and opens the browser at
     {website}/signin?next=/auth/device?...&redirect_uri=http://127.0.0.1:{port}/auth/callback
  2. the website mints a 5-minute single-use code (Supabase RPC
     issue_device_handoff) and navigates the browser to the loopback URL
  3. the listener verifies the CSRF state and hands the code to the
     controller, which exchanges code+verifier+device_id at the Supabase
     Edge Function auth-handoff-exchange for a session
  4. session.py persists the rotating refresh token in the OS keyring and
     keeps the session fresh in the background.

Modules: pkce/supabase/loopback/session are bpy-free and unit-tested;
controller.py is the Blender-facing orchestration layer. Callers use the
submodules directly (`from .auth import controller, session`); the session
singleton is `session.session`.
"""

from __future__ import annotations

# bpy-free submodules — importable in plain Python for unit tests.
from . import loopback, pkce, session, supabase

# controller imports bpy and is deliberately NOT imported here; Blender-side
# callers import it explicitly (`from .auth import controller`), and
# register() below pulls it in lazily.

__all__ = ["loopback", "pkce", "session", "supabase", "register", "unregister"]


def register() -> None:
    from . import controller
    controller.register()


def unregister() -> None:
    from . import controller
    controller.unregister()
