// SPDX-FileCopyrightText: 2026 Raz Ben Aharon
// SPDX-License-Identifier: Apache-2.0

import { expect, test } from 'claude-code/testing'

import { bandText, codexReading, repoFileTurnsCodexOff } from './quota-band'

const NOW = Date.parse('2026-10-27T18:00:00Z')
const IN_TWO_HOURS = NOW + 2 * 3600 * 1000

const BAND = (surface: 'terminal' | 'desktop' | 'vscode' | 'mobile') => ({
  plugin: 'modelquad',
  component: 'AbovePrompt',
  surface,
  props: { hasSurvey: false, bodyColumns: 120 },
}) as const

const file = (collected: number, extra: Record<string, unknown> = {}) => JSON.stringify({
  available: true,
  five_hour_remaining_percentage: 90,
  five_hour_resets_at: Math.floor(IN_TWO_HOURS / 1000),
  seven_day_used_percentage: 30,
  collected_at: new Date(collected).toISOString(),
  ...extra,
})

const UNKNOWN_BAND = 'Codex 5h unknown / 7d unknown'

test("Codex reads ModelQuad's reading, as left, in the status line's words", async () => {
  const text = bandText(codexReading(file(NOW - 60_000)), NOW)
  expect(text.startsWith('Codex 5h 90% left, resets ')).toBe(true)
  expect(text.endsWith(' / 7d 70% left')).toBe(true)
})

test('the plan Codex reports is named, and only one the band knows', async () => {
  expect(bandText(codexReading(file(NOW, { plan_type: 'plus' })), NOW)
    .startsWith('Codex (Plus) 5h 90% left')).toBe(true)
  expect(bandText(codexReading(file(NOW, { plan_type: 'C:/Users/me' })), NOW)
    .startsWith('Codex 5h 90% left')).toBe(true)
  expect(bandText(codexReading(file(NOW, { plan_type: 42 })), NOW)
    .startsWith('Codex 5h 90% left')).toBe(true)
})

test('an old reading says when it was taken, once', async () => {
  const text = bandText(codexReading(file(NOW - 3600_000)), NOW)
  expect(text.includes(' (as of ')).toBe(true)
  expect(text.split('as of').length).toBe(2)
})

test('no reading is said in words, never as 0%', async () => {
  expect(bandText(codexReading('not json'), NOW)).toBe(UNKNOWN_BAND)
  expect(bandText(codexReading('{"available": false}'), NOW)).toBe(UNKNOWN_BAND)
  expect(bandText(null, NOW)).toBe(UNKNOWN_BAND)
  expect(bandText(codexReading(file(NOW, { five_hour_remaining_percentage: null })), NOW)
    .startsWith('Codex 5h unknown / 7d 70% left')).toBe(true)
})

test('signed out reads as a step to take, not as a number', async () => {
  const text = bandText(codexReading('{"available": true, "quota_error": "Not logged in. Run codex login."}'), NOW)
  expect(text).toBe('Codex: not signed in (run codex login)')
  expect(text.includes('%')).toBe(false)
})

test('a window that has reset is not shown as its old figure', async () => {
  const reset = file(NOW, { five_hour_resets_at: Math.floor((NOW - 1000) / 1000) })
  expect(bandText(codexReading(reset), NOW).startsWith('Codex 5h unknown / 7d')).toBe(true)
})

test('a repository file that turns Codex off is said after the quota', async () => {
  const text = bandText(codexReading(file(NOW)), NOW, { repoOff: true })
  expect(text.endsWith(' | Codex is off in this repository (.modelquad.toml)')).toBe(true)
  expect(bandText(null, NOW, { repoOff: true }))
    .toBe(`${UNKNOWN_BAND} | Codex is off in this repository (.modelquad.toml)`)
})

test('only a plain allowed = false in [codex] counts as off', async () => {
  expect(repoFileTurnsCodexOff('schema = 1\n[codex]\nallowed = false # no Codex here\n')).toBe(true)
  expect(repoFileTurnsCodexOff('schema = 1\n[codex]\nallowed = true\n')).toBe(false)
  expect(repoFileTurnsCodexOff('[offers]\nallowed = false\n')).toBe(false)
  expect(repoFileTurnsCodexOff('[codex]\n# allowed = false\n')).toBe(false)
  expect(repoFileTurnsCodexOff('x = """\n[codex]\nallowed = false\n"""\n')).toBe(false)
})

test('the band draws beside the prompt, but not in the terminal', async ($, on) => {
  on('clock.now', () => ({ value: NOW }))
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })

  for (const surface of ['desktop', 'vscode', 'mobile'] as const) {
    const ui = await $.ui.mount(BAND(surface))
    expect((await ui.find({ type: 'Text', text: /Codex/ }))?.text).toBe(UNKNOWN_BAND)
    expect(await ui.find({ type: 'Text', text: /Claude/ })).toBeUndefined()
    await ui.unmount()
  }

  const terminal = await $.ui.mount(BAND('terminal'))
  expect((await terminal.find({ type: 'Text', text: /engine/ }))?.text).toBe('engine')
  expect(await terminal.find({ type: 'Text', text: /Codex/ })).toBeUndefined()
  await terminal.unmount()
})
