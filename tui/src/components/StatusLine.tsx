import React from "react";
import { Box, Text } from "ink";
import Spinner from "ink-spinner";
import type { Status } from "../types.js";

interface Props {
  status: Status;
  worker: string;
}

// One thin line. Compact dim text on the right, status dot/spinner on
// the left. No border. Designed to disappear when nothing's happening.
export function StatusLine({ status, worker }: Props) {
  let color: string;
  let label: string;
  let withSpinner = false;

  switch (status.kind) {
    case "connecting":
      color = "yellow"; label = "connecting"; withSpinner = true; break;
    case "idle":
      color = "green"; label = "ready"; break;
    case "thinking":
      color = "cyan"; label = "thinking"; withSpinner = true; break;
    case "streaming":
      color = "cyan"; label = "streaming"; withSpinner = true; break;
    case "error":
      color = "red"; label = `error · ${status.message}`; break;
    case "exited":
      color = "red"; label = `disconnected · code ${status.code ?? "?"}`; break;
  }

  return (
    <Box>
      <Box marginRight={1}>
        {withSpinner ? (
          <Text color={color}><Spinner type="dots" /></Text>
        ) : (
          <Text color={color}>●</Text>
        )}
      </Box>
      <Text color={color}>{label}</Text>
      <Box flexGrow={1} />
      <Text dimColor>{worker} · /help</Text>
    </Box>
  );
}
