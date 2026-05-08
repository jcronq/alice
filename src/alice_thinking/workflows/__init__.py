"""Stage workflows for thinking — Stage B is the first.

Each workflow is a typed-step graph composed via google-adk's
``SequentialAgent`` / ``ParallelAgent``. The ``intelligence'' sits in
LLM subroutines wrapped by deterministic apply helpers; the
orchestration is plain code with per-step timeouts and structured
telemetry.
"""
