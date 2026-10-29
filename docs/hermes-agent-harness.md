# Hermes Agent harness (experimental)

This is NousResearch's Hermes Agent CLI, not the legacy `backend: hermes`
HTTP model shim. It executes its own tool loop and sends JSONL events to Alice's
Kernel adapter. No production configuration is changed by installing the code.

Prerequisites: install a compatible Hermes Agent release in the runtime container
and expose the `hermes` executable on PATH (or set `ALICE_HERMES_BIN`). The
adapter uses `hermes chat --query-file - --oneshot --format stream-json`, available
in NousResearch/hermes-agent at commit e298669b44ce (checked 2026-10-29).
The CLI needs its gateway running; validate gateway startup and a full one-shot
turn before switching config. Install from the pinned upstream checkout with
`./setup-hermes.sh --runtime-only`, not `pip install` or `uv tool install`: the
upstream lockfile intentionally overrides the browser-harness Pillow pin and
the regular wheel build is unsupported. On Debian trixie, install `libatomic1`
before setup (the pinned Node binary requires it). The resulting launcher is
`<checkout>/.hermes/bin/hermes`; set `ALICE_HERMES_BIN` to that path. Start the
gateway with the same `HERMES_HOME`, `OPENAI_BASE_URL`, and `OPENAI_API_KEY`
that the adapter uses; use a dedicated, persistent `HERMES_HOME` to avoid
stale sessions or locks from other test runs. Verified with the upstream pinned
installer and a successful Qwen one-shot on 2026-10-29. No binary or credentials
are bundled here.

To compare thinking on the same model as pi-mono, keep the existing
`thinking.model: litellm/qwen-local` and `thinking.backend: pi`, set
`thinking.harness: hermes-agent`, and set
`backends.pi.base_url: http://alice-litellm:4000/v1` in
`~/alice-mind/config/model.yml`. Revert harness to `pi-mono` to switch back.
The Hermes adapter sends model `qwen-local` to that endpoint. An absent endpoint
is an error, never an implicit call to an external default provider. For the
configured LiteLLM URL, it forwards `LITELLM_MASTER_KEY` as the scoped OpenAI
credential; the Hermes gateway must be launched with the same environment.

Only thinking is supported. Speaking requires a Signal send_message tool bridge
that Hermes Agent does not have; selecting it fails closed rather than silently
losing replies. Hermes' `development` toolset does not offer Alice's MCP tools,
so MCP tools and SDK hooks are reported as dropped in events. Its CLI owns its
own prompt/session/tool semantics; this is a harness comparison, not a claim of
tool or skill parity. Keep pi-mono as the live default until a real smoke test
and comparison run has completed.
