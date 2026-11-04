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
// on disk (usage-codex.json), and the repository note is read from the
// project's own .modelquad.toml. A missing or unreadable figure reads
// "unknown", never 0%. The words match the status line: "Codex 5h 90% left,
// resets 20:00 / 7d 70% left", an old reading ends with "(as of 14:02)".
// Plain text only: no colour carries meaning, so a screen reader reads it all.

import type { EngineInterface, Register } from 'claude-code'

/** One window: the percent left, and when it resets (epoch ms), or unknown. */
type Window = { left: number | null; resetsAt: number | null }
type Reading = {
  five: Window
  week: Window
  asOf: number | null
  /** The plan Codex itself reported, as a person reads it; null when not one we know. */
  plan: string | null
}
/** What the band knows when there is no reading to show. */
type NoReading = { signedOut: boolean; plan: string | null }

const UNKNOWN = 'unknown'
const REPO_FILE = '.modelquad.toml'
/** At most this many folders up from the session's folder to find `.git`. */
const MAX_FOLDERS_UP = 32

/** The server refreshes Codex every 4 minutes; older than this is said. */
const STALE_MS = 15 * 60 * 1000
const READ_EVERY_MS = 60 * 1000
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

/**
 * Plan names Codex reports, as a person reads them. Human-only: the band is
 * never read by the model. A value outside this list shows no label.
 */
const PLAN_LABELS: Record<string, string> = {
  free: 'Free',
  go: 'Go',
  plus: 'Plus',
  pro: 'Pro',
  prolite: 'Pro Lite',
  team: 'Team',
  business: 'Business',
  enterprise: 'Enterprise',
  edu: 'Edu',
  education: 'Edu',
  k12: 'K-12',
}

/** Words Codex uses when nobody is signed in; the status line reads the same. */
const SIGN_IN_WORDS = ['not logged in', 'log in', 'login', 'logged out', 'sign in', 'signed in',
  'unauthorized', 'unauthenticated', 'authentication', '401']

let codex: Reading | NoReading = { signedOut: false, plan: null }
let repoOff = false
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

function planLabel(value: unknown): string | null {
  if (typeof value !== 'string') return null
  return PLAN_LABELS[value.trim().toLowerCase()] ?? null
}

function isReading(value: Reading | NoReading | null): value is Reading {
  return value !== null && 'five' in value
}

/** Codex's windows from ModelQuad's own reading; what is known otherwise. */
export function codexReading(text: string): Reading | NoReading {
  let data: Record<string, unknown>
  try {
    const parsed = JSON.parse(text)
    if (!parsed || typeof parsed !== 'object') return { signedOut: false, plan: null }
    data = parsed as Record<string, unknown>
  } catch {
    return { signedOut: false, plan: null }
  }
  const plan = planLabel(data.plan_type)
  if (data.available !== true || data.quota_error) {
    const why = `${data.quota_error ?? ''} ${data.reason ?? ''}`.toLowerCase()
    return { signedOut: SIGN_IN_WORDS.some(word => why.includes(word)), plan }
  }
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
    plan,
  }
}

/**
 * Whether a repository file turns Codex off: `allowed = false` in its
 * `[codex]` table. ModelQuad refuses Codex for such a file whether or not
 * the rest of it is valid, so saying "off" is never wrong; anything the band
 * cannot read for certain says nothing (ModelQuad still checks every call).
 */
export function repoFileTurnsCodexOff(text: string): boolean {
  if (text.includes("'''") || text.includes('"""')) return false
  let table = ''
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/#.*$/, '').trim()
    if (line === '') continue
    const header = /^\[\s*([A-Za-z0-9_.-]+)\s*\]$/.exec(line)
    if (header) {
      table = header[1]
      continue
    }
    if (table === 'codex' && /^allowed\s*=\s*false$/.test(line)) return true
  }
  return false
}

function clock(ms: number, now: number): string {
  const at = new Date(ms)
  const time = `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`
  // Another day reads like the status line: "Tue 15:00".
  if (at.toDateString() === new Date(now).toDateString()) return time
  return `${WEEKDAYS[at.getDay()]} ${time}`
}

function showsNumber(window: Window, now: number): boolean {
  // A reading of a window that has since reset says nothing about the new one.
  return window.left !== null && (window.resetsAt === null || window.resetsAt > now)
}

function windowText(window: Window, now: number): string {
  return showsNumber(window, now) ? `${(window.left as number).toFixed(0)}% left` : UNKNOWN
}

/** The band: the same words and order as ModelQuad's status line uses for Codex. */
export function bandText(reading: Reading | NoReading | null, now: number, options: { repoOff?: boolean } = {}): string {
  const name = reading?.plan ? `Codex (${reading.plan})` : 'Codex'
  const repo = options.repoOff ? ` | Codex is off in this repository (${REPO_FILE})` : ''
  if (!isReading(reading)) {
    const state = reading?.signedOut ? `${name}: not signed in (run codex login)` : `${name} 5h ${UNKNOWN} / 7d ${UNKNOWN}`
    return state + repo
  }
  let five = windowText(reading.five, now)
  if (showsNumber(reading.five, now) && reading.five.resetsAt !== null) {
    five += `, resets ${clock(reading.five.resetsAt, now)}`
  }
  const week = windowText(reading.week, now)
  const shown = showsNumber(reading.five, now) || showsNumber(reading.week, now)
  const old = shown && reading.asOf !== null && now - reading.asOf > STALE_MS
  const asOf = old ? ` (as of ${clock(reading.asOf as number, now)})` : ''
  return `${name} 5h ${five} / 7d ${week}${asOf}${repo}`
}

async function readCodex($: EngineInterface): Promise<void> {
  const home = (await $.env.get('USERPROFILE')) || (await $.env.get('HOME'))
  let next: Reading | NoReading = { signedOut: false, plan: null }
  if (home) {
    try {
      next = codexReading(await $.fs.read(`${home}/.claude/usage-codex.json`))
    } catch {
      // no reading yet, or not readable: unknown
    }
  }
  codex = next
  repoOff = await readRepoOff($)
  $.ui.invalidate('ui.render')
}

/** The repository file of the session's folder: its Git top folder, or the folder itself. */
async function readRepoOff($: EngineInterface): Promise<boolean> {
  try {
    const start = (await $.session.cwd()).replace(/[\\/]+$/, '')
    let folder = start
    for (let up = 0; up < MAX_FOLDERS_UP; up += 1) {
      if (await $.fs.exists(`${folder}/.git`)) break
      const cut = Math.max(folder.lastIndexOf('/'), folder.lastIndexOf('\\'))
      if (cut <= 0 || /^[A-Za-z]:$/.test(folder)) {
        folder = start // outside Git: the session's own folder
        break
      }
      folder = folder.slice(0, cut)
    }
    const path = `${folder}/${REPO_FILE}`
    const stat = await $.fs.stat(path)
    // ModelQuad never follows a link for this file; neither does the band.
    if (stat.isLink || stat.kind !== 'file' || stat.size > 8 * 1024) return false
    return repoFileTurnsCodexOff(await $.fs.read(path))
  } catch {
    return false // no file, or not readable here: nothing to say
  }
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
        <Text dimColor>{bandText(codex, await $.clock.now(), { repoOff })}</Text>
      </Box>
    )
  })
}
