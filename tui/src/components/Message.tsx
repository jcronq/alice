import React from "react";
import { Box, Text } from "ink";
import type { Message as Msg } from "../types.js";

interface Props {
  msg: Msg;
}

// One row in the conversation. Role-driven coloring + tag.
// Kept deliberately simple: no markdown rendering — Alice's chunked
// output is shown as-is. Code blocks survive because they're already
// indented with leading whitespace by the model in most cases.
export function MessageView({ msg }: Props) {
  switch (msg.role) {
    case "user":
      return (
        <Box flexDirection="column" marginTop={1}>
          <Text color="magenta" bold>{`▌ you`}</Text>
          <Box paddingLeft={2}>
            <Text>{msg.text}</Text>
          </Box>
        </Box>
      );

    case "alice": {
      const footer: string[] = [];
      if (typeof msg.durationMs === "number") footer.push(`${(msg.durationMs / 1000).toFixed(1)}s`);
      if (typeof msg.costUsd === "number") footer.push(`$${msg.costUsd.toFixed(4)}`);
      return (
        <Box flexDirection="column" marginTop={1}>
          <Text color="cyan" bold>{`▌ alice`}{msg.streaming ? " ·" : ""}</Text>
          <Box paddingLeft={2}>
            <Text>{msg.text}</Text>
          </Box>
          {footer.length > 0 && (
            <Box paddingLeft={2}>
              <Text dimColor>{footer.join("  ·  ")}</Text>
            </Box>
          )}
        </Box>
      );
    }

    case "tool": {
      const summary = formatToolUse(msg.toolName ?? "tool", msg.toolInput);
      return (
        <Box paddingLeft={2}>
          <Text color="gray">{`⚙ ${summary}`}</Text>
        </Box>
      );
    }

    case "error":
      return (
        <Box flexDirection="column" marginTop={1}>
          <Text color="red" bold>{`▌ error`}</Text>
          <Box paddingLeft={2}>
            <Text color="red">{msg.text}</Text>
          </Box>
        </Box>
      );

    case "system":
      return (
        <Box paddingLeft={2}>
          <Text dimColor italic>{msg.text}</Text>
        </Box>
      );
  }
}

// One-line summary like "Bash(command: ls -la)" or "Read(file_path: …/CLAUDE.md)".
// The daemon already truncated the value; we just format it.
function formatToolUse(name: string, input?: Record<string, unknown>): string {
  if (!input) return name;
  const keys = Object.keys(input);
  if (keys.length === 0) return name;
  const k = keys[0]!;
  const v = String(input[k] ?? "");
  return `${name}(${k}: ${v})`;
}
