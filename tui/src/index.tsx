import React from "react";
import { render } from "ink";
import { App } from "./App.js";

interface Opts {
  worker: string;
  socket?: string;
}

function parseArgs(argv: string[]): Opts {
  let worker = "alice-worker-blue";
  let socket: string | undefined;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--worker" && i + 1 < argv.length) {
      worker = argv[++i]!;
    } else if (a === "--socket" && i + 1 < argv.length) {
      socket = argv[++i]!;
    } else if (a === "-h" || a === "--help") {
      process.stdout.write(USAGE);
      process.exit(0);
    }
  }
  return { worker, socket };
}

const USAGE = `Usage: alice-tui [--worker NAME] [--socket PATH]

  Pretty TUI for talking to Alice. Spawns
  \`docker exec -i WORKER alice-client --json\` and renders the
  event stream as a chat-style interface.

Options:
  --worker NAME    Container name (default: alice-worker-blue)
  --socket PATH    Override the socket path inside the container
                   (default: /tmp/alice.sock)
  -h, --help       Show this message
`;

const opts = parseArgs(process.argv.slice(2));
render(<App worker={opts.worker} socket={opts.socket} />);
