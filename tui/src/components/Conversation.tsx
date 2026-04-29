import React from "react";
import { Box, Static } from "ink";
import { MessageView } from "./Message.js";
import type { Message } from "../types.js";

interface Props {
  messages: Message[];
}

// Scrollback. We split into two regions:
//   - Static: completed messages — Ink renders these once and never
//     re-renders, so the terminal scrollback is preserved (you can
//     scroll up with your mouse like in any normal terminal output).
//   - Live: the in-flight alice message (streaming=true). Re-rendered
//     in place as chunks arrive; once `done` lands the hook flips
//     streaming=false and it migrates into the Static region on the
//     next render.
export function Conversation({ messages }: Props) {
  const live: Message[] = [];
  const settled: Message[] = [];
  for (const m of messages) {
    if (m.role === "alice" && m.streaming) {
      live.push(m);
    } else {
      settled.push(m);
    }
  }

  return (
    <>
      <Static items={settled}>
        {(m) => <MessageView key={m.id} msg={m} />}
      </Static>
      {live.length > 0 && (
        <Box flexDirection="column">
          {live.map((m) => <MessageView key={m.id} msg={m} />)}
        </Box>
      )}
    </>
  );
}
