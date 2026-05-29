"""
Animora evaluation harness.

Runs a fixed battery of prompts against the orchestrator and produces a
scorecard so we can measure whether changes to the master prompt, the
personas, the router, or the validator actually improve output — instead
of finding out by burning credits in the live UI.

See `ai-backend/eval/runner.py --help` for usage.
"""
