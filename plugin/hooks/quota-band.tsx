// SPDX-FileCopyrightText: 2026 Raz Ben Aharon
// SPDX-License-Identifier: Apache-2.0
//
// ModelQuad's quota band: Claude's and Codex's 5-hour and weekly windows above
// the prompt, on the surfaces that draw no status line (the desktop app, an
// IDE, a phone). The terminal keeps its status line, so nothing is drawn there.
//
// Display only. It hooks no tool call, prompt or approval, starts nothing and
// runs nothing: Claude's figures are the ones Claude Code itself measures, and
// Codex's come from the reading ModelQuad's server already keeps on disk
// (usage-codex.json). A missing or unreadable figure reads "unknown", never 0%.

import type { EngineInterface, Register, SessionRateLimit } from 'claude-code'

/** One window: the percent left, and when it resets (epoch ms), or unknown. */
type Window = { left: number | null; resetsAt: number | null }
type Reading = { five: Window; week: Window; asOf: number | null }

/** The words ModelQuad's status line uses for the same states. */
const FIRST_MESSAGE = 'shows after your first message'
const NEXT_MESSAGE = 'shows after your next message'
const UNKNOWN = 'unknown'

/** The server refreshes Codex every 4 minutes; older than this is said. */
const CODEX_STALE_MS = 15 * 60 * 1000
const CODEX_READ_EVERY_MS = 60 * 1000

const UNKNOWN_WINDOW: Window = { left: null, resetsAt: null }

let claude: Reading | null = null
let codex: Reading | null = null
let reader: { cancel: () => void } | null = null

export function clamp(percent: number): number {
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

/** Claude's windows as Claude Code measured them on the last response. */
export function claudeReading(limits: readonly SessionRateLimit[]): Reading | null {
  const window = (kind: string): Window => {
    const limit = limits.find(l => l.kind === kind)
    if (!limit || typeof limit.percentUsed !== 'number') return UNKNOWN_WINDOW
    return { left: clamp(100 - limit.percentUsed), resetsAt: epochMs(limit.resetsAt) }
  }
  const reading = { five: window('five_hour'), week: window('seven_day'), asOf: null }
  return reading.five.left === null && reading.week.left === null ? null : reading
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

function windowText(window: Window, now: number, afterReset: string): string {
  if (window.left === null) return UNKNOWN
  if (window.resetsAt !== null && window.resetsAt <= now) return afterReset
  return `${window.left.toFixed(0)}% left`
}

/** One provider's part, worded as ModelQuad's status line words it. */
export function providerText(label: string, reading: Reading | null, now: number): string {
  const isClaude = label === 'Claude'
  if (reading === null) return isClaude ? `Claude: ${FIRST_MESSAGE}` : `Codex: ${UNKNOWN}`
  const afterReset = isClaude ? NEXT_MESSAGE : UNKNOWN
  let five = windowText(reading.five, now, afterReset)
  if (five.endsWith('left') && reading.five.resetsAt !== null) {
    five += `, resets ${clock(reading.five.resetsAt)}`
  }
  const stale = !isClaude && reading.asOf !== null && now - reading.asOf > CODEX_STALE_MS
  const week = windowText(reading.week, now, afterReset)
  return `${label} 5h ${five} / 7d ${week}${stale ? ` (as of ${clock(reading.asOf as number)})` : ''}`
}

export function bandText(now: number): string {
  return `${providerText('Claude', claude, now)} | ${providerText('Codex', codex, now)}`
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
    claude = claudeReading((await $.session.usage()).rateLimits)
    await readCodex($)
    reader?.cancel()
    reader = $.clock.every(CODEX_READ_EVERY_MS, () => void readCodex($))
    return result
  })

  on('session.measure', ($, e, next) => {
    if (e.changed.includes('rateLimits')) {
      claude = claudeReading(e.rateLimits)
      $.ui.invalidate('ui.render')
    }
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    // The terminal has ModelQuad's status line; draw only where it is missing.
    if (e.surface === 'terminal' || e.props.hasSurvey) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box>
        <Text dimColor>{bandText(await $.clock.now())}</Text>
      </Box>
    )
  })
}
