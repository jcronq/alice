import React, { useState, useCallback } from "react";
import { Box, Text, useInput } from "ink";
import TextInput from "ink-text-input";

interface Props {
  onSubmit: (text: string) => void;
  // Disable input while alice is mid-turn so we don't accidentally
  // queue a second prompt before the first is acknowledged.
  disabled: boolean;
  // Callback for Ctrl-L (clear scrollback).
  onClear?: () => void;
}

// Bottom prompt. Single thin separator above, then a `❯` prompt + text
// input. No box border — borders make the TUI feel cluttered when most
// of the canvas is conversation text.
export function InputBar({ onSubmit, disabled, onClear }: Props) {
  const [value, setValue] = useState("");
  const [history, setHistory] = useState<string[]>([]);
  const [histIdx, setHistIdx] = useState<number | null>(null);

  // History navigation. Up/Down only fires when input is empty OR we're
  // already in history mode, to avoid hijacking arrow keys mid-edit.
  useInput((input, key) => {
    if (disabled) return;
    if (key.upArrow) {
      if (history.length === 0) return;
      const next = histIdx === null ? history.length - 1 : Math.max(0, histIdx - 1);
      setHistIdx(next);
      setValue(history[next] ?? "");
      return;
    }
    if (key.downArrow) {
      if (histIdx === null) return;
      const next = histIdx + 1;
      if (next >= history.length) {
        setHistIdx(null);
        setValue("");
      } else {
        setHistIdx(next);
        setValue(history[next] ?? "");
      }
      return;
    }
    if (key.ctrl && input === "l") {
      onClear?.();
    }
  });

  const handleSubmit = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;
      setHistory((h) => (h[h.length - 1] === trimmed ? h : [...h, trimmed]));
      setHistIdx(null);
      setValue("");
      onSubmit(trimmed);
    },
    [onSubmit]
  );

  return (
    <Box flexDirection="row">
      <Box marginRight={1}>
        <Text color={disabled ? "gray" : "magenta"} bold>❯</Text>
      </Box>
      {disabled ? (
        <Text dimColor italic>…</Text>
      ) : (
        <TextInput
          value={value}
          onChange={setValue}
          onSubmit={handleSubmit}
          placeholder=""
        />
      )}
    </Box>
  );
}
