// SPDX-FileCopyrightText: 2026 Raz Ben Aharon
// SPDX-License-Identifier: Apache-2.0

import { expect, test } from 'claude-code/testing'

import { claudeReading, codexReading, providerText } from './quota-band'

const NOW = Date.parse('2026-10-27T18:00:00Z')
const IN_TWO_HOURS = NOW + 2 * 3600 * 1000

const BAND = (surface: 'terminal' | 'desktop' | 'vscode' | 'mobile') => ({
  plugin: 'modelquad',
  component: 'AbovePrompt',
  surface,
  props: { hasSurvey: false, bodyColumns: 120 },
}) as const

test('Claude reads as left, from what Claude Code measured', async () => {
  const reading = claudeReading([
    { kind: 'five_hour', percentUsed: 38.4, resetsAt: new Date(IN_TWO_HOURS).toISOString() },
    { kind: 'seven_day', percentUsed: 20 },
  ])
  const text = providerText('Claude', reading, NOW)
  expect(text.startsWith('Claude 5h 62% left, resets ')).toBe(true)
  expect(text.endsWith(' / 7d 80% left')).toBe(true)
})

test('no reading is said in words, never as 0%', async () => {
  expect(providerText('Claude', claudeReading([]), NOW)).toBe('Claude: shows after your first message')
  expect(providerText('Codex', codexReading('not json'), NOW)).toBe('Codex: unknown')
  expect(providerText('Codex', codexReading('{"available": false}'), NOW)).toBe('Codex: unknown')
  expect(providerText('Codex', codexReading('{"available": true, "quota_error": "signed out"}'), NOW))
    .toBe('Codex: unknown')
})

test("Codex reads ModelQuad's reading, and says when it is old", async () => {
  const file = (collected: number) => JSON.stringify({
    available: true,
    five_hour_remaining_percentage: 90,
    five_hour_resets_at: Math.floor(IN_TWO_HOURS / 1000),
    seven_day_used_percentage: 30,
    collected_at: new Date(collected).toISOString(),
  })
  const fresh = providerText('Codex', codexReading(file(NOW - 60_000)), NOW)
  expect(fresh.startsWith('Codex 5h 90% left, resets ')).toBe(true)
  expect(fresh.endsWith(' / 7d 70% left')).toBe(true)
  expect(providerText('Codex', codexReading(file(NOW - 3600_000)), NOW).includes('(as of ')).toBe(true)
})

test('a window that has reset is not shown as its old figure', async () => {
  const reading = claudeReading([
    { kind: 'five_hour', percentUsed: 90, resetsAt: new Date(NOW - 1000).toISOString() },
    { kind: 'seven_day', percentUsed: 10 },
  ])
  expect(providerText('Claude', reading, NOW).startsWith('Claude 5h shows after your next message')).toBe(true)
})

test('the band draws beside the prompt, but not in the terminal', async ($, on) => {
  on('clock.now', () => ({ value: NOW }))
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })

  for (const surface of ['desktop', 'vscode', 'mobile'] as const) {
    const ui = await $.ui.mount(BAND(surface))
    const row = await ui.find({ type: 'Text', text: /Claude/ })
    expect(row?.text).toBe('Claude: shows after your first message | Codex: unknown')
    await ui.unmount()
  }

  const terminal = await $.ui.mount(BAND('terminal'))
  expect((await terminal.find({ type: 'Text', text: /engine/ }))?.text).toBe('engine')
  expect(await terminal.find({ type: 'Text', text: /Claude/ })).toBeUndefined()
  await terminal.unmount()
})
