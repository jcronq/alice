"""Workflow harnesses for thinking-side wakes.

Per ``docs/designs/stage-b-adk-workflow-sketch.md``, certain wake phases
(today: Stage B; future: Stage C) are checklist-driven enough that the
intelligence sits in the side-effects rather than in the orchestration.
Those phases live as native Python workflows under this package — typed
steps, deterministic flow, LLM subroutines for parts that need judgement.

Generative phases (Stage D synthesis, active mode) stay prompt-driven and
do NOT live here.
"""
