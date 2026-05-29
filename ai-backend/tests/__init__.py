"""End-to-end smoke tests for the Animora AI backend.

Run from the ai-backend/ directory:
  python -m tests.test_call               # direct AnthropicClient ping
  python -m tests.test_ws                 # full WebSocket protocol
  python -m tests.test_phase4_classifier  # intent classifier accuracy (11 cases)
"""
