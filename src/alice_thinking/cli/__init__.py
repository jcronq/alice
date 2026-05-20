"""Thinking-side command-line entry points.

Pi-mono (kernels.pi.kernel.PiKernel) strips ``mcp__``-prefixed tools at
permission-translation time, so the ``run_experiment`` MCP tool the
AnthropicKernel exposes is invisible to the local Qwen runtime that
drives thinking's sleep wakes. This package exposes the same dispatch
machinery as a Bash-callable CLI so pi-driven wakes can dispatch
experiments via ``alice-experiment`` instead.

The CLI is a thin shell over :class:`alice_thinking.experiments.ExperimentRunner`
— same runner, same card writer, same surface-emission pipeline.
"""
