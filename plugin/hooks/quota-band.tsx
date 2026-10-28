// SPDX-FileCopyrightText: 2026 Raz Ben Aharon
// SPDX-License-Identifier: Apache-2.0
//
// ModelQuad's quota band: Codex's 5-hour and weekly windows above the prompt,
// on the surfaces that draw no status line (the desktop app, an IDE, a phone).
// Claude's own usage is already on show there, so the band says Codex alone.
// The terminal keeps ModelQuad's status line, so nothing is drawn there.
//
// Display only. It hooks no tool call, prompt or approval, starts nothing and
// runs nothing: the figures are the reading ModelQuad's server already keeps
// on disk (usage-codex.json). A missing or unreadable figure reads "unknown",
// never 0%.

import type { EngineInterface, Register } from 'claude-code'

/** One window: the percent left, and when it resets (epoch ms), or unknown. */
type Window = { left: number | null; resetsAt: number | null }
type Reading = { five: Window; week: Window; asOf: number | null }

const UNKNOWN = 'unknown'

/** The server refreshes Codex every 4 minutes; older than this is said. */
const STALE_MS = 15 * 60 * 1000
const READ_EVERY_MS = 60 * 1000

let codex: Reading | null = null
let reader: { cancel: () => void } | null = null

function clamp(percent: number): number {
  return Math.min(100, Math.max(0, percent))
}

function epochMs(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) {
    // ModelQuad writes resets as epoch seconds.
    return value < 1e12 ? value * 1000 : value
  }
  if (typeof value === 'string') {
    const ms = Date.parse(value)
    return Number.isNaN(ms) ? null : ms
  }
  return null
}

/** Codex's windows from ModelQuad's own reading; null when there is none. */
export function codexReading(text: string): Reading | null {
  let data: Record<string, unknown>
  try {
    const parsed = JSON.parse(text)
    if (!parsed || typeof parsed !== 'object') return null
    data = parsed as Record<string, unknown>
  } catch {
    return null
  }
  if (data.available !== true || data.quota_error) return null
  const window = (prefix: string): Window => {
    const remaining = data[`${prefix}_remaining_percentage`]
    const used = data[`${prefix}_used_percentage`]
    const left =
      typeof remaining === 'number' ? clamp(remaining)
        : typeof used === 'number' ? clamp(100 - used)
          : null
    return { left, resetsAt: epochMs(data[`${prefix}_resets_at`]) }
  }
  return {
    five: window('five_hour'),
    week: window('seven_day'),
    asOf: epochMs(data.collected_at ?? data.observed_at),
  }
}

function clock(ms: number): string {
  const at = new Date(ms)
  return `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`
}

function windowText(window: Window, now: number): string {
  if (window.left === null) return UNKNOWN
  // A reading of a window that has since reset says nothing about the new one.
  if (window.resetsAt !== null && window.resetsAt <= now) return UNKNOWN
  return `${window.left.toFixed(0)}% left`
}

/** The band, worded as ModelQuad's status line words Codex. */
export function bandText(reading: Reading | null, now: number): string {
  if (reading === null) return `Codex: ${UNKNOWN}`
  let five = windowText(reading.five, now)
  if (five.endsWith('left') && reading.five.resetsAt !== null) {
    five += `, resets ${clock(reading.five.resetsAt)}`
  }
  const stale = reading.asOf !== null && now - reading.asOf > STALE_MS
  const week = windowText(reading.week, now)
  return `Codex 5h ${five} / 7d ${week}${stale ? ` (as of ${clock(reading.asOf as number)})` : ''}`
}

async function readCodex($: EngineInterface): Promise<void> {
  const home = (await $.env.get('USERPROFILE')) || (await $.env.get('HOME'))
  let next: Reading | null = null
  if (home) {
    try {
      next = codexReading(await $.fs.read(`${home}/.claude/usage-codex.json`))
    } catch {
      next = null // no reading yet, or not readable: unknown
    }
  }
  codex = next
  $.ui.invalidate('ui.render')
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const result = await next(e)
    await readCodex($)
    reader?.cancel()
    reader = $.clock.every(READ_EVERY_MS, () => void readCodex($))
    return result
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    // The terminal has ModelQuad's status line; draw only where it is missing.
    if (e.surface === 'terminal' || e.props.hasSurvey) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box>
        <Text dimColor>{bandText(codex, await $.clock.now())}</Text>
      </Box>
    )
  })
}
