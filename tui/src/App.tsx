import React, { useCallback } from "react";
import { Box, Text, useApp, useInput } from "ink";
import { Conversation } from "./components/Conversation.js";
import { InputBar } from "./components/InputBar.js";
import { StatusLine } from "./components/StatusLine.js";
import { useAliceClient } from "./hooks/useAliceClient.js";

export interface AppProps {
  worker: string;
  socket?: string;
}

const HELP_TEXT = [
  "  /help          show this",
  "  /clear         clear scrollback",
  "  /quit          exit (Ctrl-C also works)",
  "  ↑ / ↓          history",
  "  Ctrl-L         clear scrollback",
].join("\n");

export function App({ worker, socket }: AppProps) {
  const { exit } = useApp();
  const { status, messages, send, clear, note } = useAliceClient({ worker, socket });

  const handleSubmit = useCallback(
    (text: string) => {
      if (text === "/help" || text === "/?") {
        note(HELP_TEXT);
        return;
      }
      if (text === "/clear") {
        clear();
        return;
      }
      if (text === "/quit" || text === "/exit") {
        exit();
        return;
      }
      send(text);
    },
    [send, clear, exit, note]
  );

  // Ctrl-C: explicit so the hook's effect cleanup kills the docker
  // exec subprocess before the process tears down.
  useInput((input, key) => {
    if (key.ctrl && input === "c") {
      exit();
    }
  });

  const inputDisabled =
    status.kind === "thinking" ||
    status.kind === "streaming" ||
    status.kind === "connecting";

  return (
    <Box flexDirection="column">
      <Conversation messages={messages} />
      <Box flexDirection="column" marginTop={1}>
        <InputBar onSubmit={handleSubmit} disabled={inputDisabled} onClear={clear} />
        <StatusLine status={status} worker={worker} />
      </Box>
    </Box>
  );
}
