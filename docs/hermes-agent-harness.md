# Hermes Agent harness (experimental)

This is NousResearch's Hermes Agent CLI, not the legacy `backend: hermes`
HTTP model shim. It executes its own tool loop and sends JSONL events to Alice's
Kernel adapter. No production configuration is changed by installing the code.

Prerequisites: install a compatible Hermes Agent release in the runtime container
and expose the `hermes` executable on PATH (or set `ALICE_HERMES_BIN`). The
adapter uses `hermes chat --query-file - --oneshot --format stream-json`, available
in NousResearch/hermes-agent at commit e298669b44ce (checked 2026-10-29).
No bundled Hermes binary or API credentials are provided here. Verify the CLI
and inference endpoint from inside the same container before switching config.

To compare thinking on the same model as pi-mono, keep the existing
`thinking.model: litellm/qwen-local` and `thinking.backend: pi`, set
`thinking.harness: hermes-agent`, and set
`backends.pi.base_url: http://alice-litellm:4000/v1` in
`~/alice-mind/config/model.yml`. Revert harness to `pi-mono` to switch back.
The Hermes adapter sends model `qwen-local` to that endpoint. An absent endpoint
is an error, never an implicit call to an external default provider.

Only thinking is supported. Speaking requires a Signal send_message tool bridge
that Hermes Agent does not have; selecting it fails closed rather than silently
losing replies. Hermes' `development` toolset does not offer Alice's MCP tools,
so MCP tools and SDK hooks are reported as dropped in events. Its CLI owns its
own prompt/session/tool semantics; this is a harness comparison, not a claim of
tool or skill parity. Keep pi-mono as the live default until a real smoke test
and comparison run has completed.
