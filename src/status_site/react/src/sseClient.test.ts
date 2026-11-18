import { describe, expect, it } from 'vitest'
import { decodeFrame } from './sseClient'

describe('decodeFrame', () => {
  it('parses a single-line data frame', () => {
    const ev = decodeFrame(
      'data: {"type":"STATE_SNAPSHOT","snapshot":{"running_now":{"markdown":"","rows":[]}},"ts":"2026-11-17T00:00:00Z"}',
    )
    expect(ev?.type).toBe('STATE_SNAPSHOT')
  })

  it('joins multi-line data fields (RFC EventSource pattern)', () => {
    const ev = decodeFrame('data: {"type":"RUN_STARTED",\ndata: "threadId":"a","runId":"b","ts":"t"}')
    expect(ev).toEqual({ type: 'RUN_STARTED', threadId: 'a', runId: 'b', ts: 't' })
  })

  it('ignores `: ping` heartbeat frames', () => {
    expect(decodeFrame(': ping')).toBeNull()
  })

  it('ignores event:/id:/retry: fields (type lives in the JSON)', () => {
    const ev = decodeFrame(
      'event: state\nid: 7\nretry: 1000\ndata: {"type":"STATE_SNAPSHOT","snapshot":{"running_now":{"markdown":"m","rows":[]}},"ts":"t"}',
    )
    expect(ev?.type).toBe('STATE_SNAPSHOT')
  })

  it('returns null for malformed JSON instead of throwing', () => {
    expect(decodeFrame('data: {not json')).toBeNull()
  })

  it('strips a UTF-8 BOM at the start of a frame', () => {
    const ev = decodeFrame('﻿data: {"type":"STATE_SNAPSHOT","snapshot":{"running_now":{"markdown":"","rows":[]}},"ts":"t"}')
    expect(ev?.type).toBe('STATE_SNAPSHOT')
  })
})
