// Formatting helpers.
//
// Centralised because the same number appears in a KPI card, a table cell, a chart
// axis and a tooltip, and the four drifting apart is what makes a dashboard look
// unfinished. Locale is fixed to en-GB rather than the browser's: the audience is
// Spanish and the deliverable is in Spanish/English, but mixing a browser-chosen
// thousands separator into a table that a Terraform output also prints leads to
// people believing two different numbers.

const NUMBER = new Intl.NumberFormat('en-GB')
const COMPACT = new Intl.NumberFormat('en-GB', { notation: 'compact', maximumFractionDigits: 1 })

export const CURRENCY_SYMBOL: Record<string, string> = {
  USD: '$',
  EUR: '\u20ac',
}

export function count(value: number): string {
  return NUMBER.format(Math.round(value))
}

/** Compact form for chart axes and dense cells: 1.2M rather than 1,234,567. */
export function compact(value: number): string {
  return COMPACT.format(value)
}

/**
 * Money, with enough decimals to stay honest at low volumes.
 *
 * Two decimals is wrong here: a demo day can legitimately total $0.004, and
 * rounding it to $0.00 makes a working meter look broken. So small amounts get four
 * decimals and larger ones get two, which keeps a bill-sized figure readable.
 */
export function money(value: number, currency: string): string {
  const symbol = CURRENCY_SYMBOL[currency] ?? ''
  const abs = Math.abs(value)
  const digits = abs === 0 ? 2 : abs < 1 ? 4 : 2
  const formatted = new Intl.NumberFormat('en-GB', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value)
  return symbol ? `${symbol}${formatted}` : `${formatted} ${currency}`
}

export function percent(value: number, digits = 1): string {
  return `${value.toFixed(digits)}%`
}

export function duration(ms: number): string {
  if (!ms) return '\u2013'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`
}

/** Short axis label: 09-15. The year is in the range header, not on every tick. */
export function shortDate(iso: string): string {
  return iso.slice(5)
}

export function longDate(iso: string): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString('en-GB', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  })
}

export function timestamp(iso: string): string {
  return new Date(iso).toLocaleString('en-GB', { timeZone: 'UTC', timeStyle: 'medium', dateStyle: 'medium' })
}

/**
 * Strip the inference-profile noise from a model id for display.
 *
 * eu.anthropic.claude-sonnet-4-6 -> sonnet-4-6. The prefix is identical on every
 * row, so showing it costs horizontal space in a table and legend while carrying no
 * information. Full id stays available in the tooltip and the CSV.
 */
export function modelLabel(modelId: string): string {
  return modelId
    .replace(/^(eu|us|apac|global)\./, '')
    .replace(/^anthropic\./, '')
    .replace(/^claude-/, '')
    .replace(/-v\d+(:\d+)?$/, '')
}

/** Deterministic colour per model, so a model keeps its colour across widgets. */
const PALETTE = [
  'var(--c1)',
  'var(--c2)',
  'var(--c3)',
  'var(--c4)',
  'var(--c5)',
  'var(--c6)',
  'var(--c7)',
  'var(--c8)',
]

export function colorFor(key: string, order: string[]): string {
  const index = order.indexOf(key)
  const slot = index >= 0 ? index : order.length
  return PALETTE[slot % PALETTE.length] as string
}

export const TOKEN_TYPE_LABELS: Record<string, string> = {
  input: 'Input',
  output: 'Output',
  cache_read: 'Cache read',
  cache_write: 'Cache write',
}

export const TOKEN_TYPE_COLORS: Record<string, string> = {
  input: 'var(--c1)',
  output: 'var(--c2)',
  cache_read: 'var(--c4)',
  cache_write: 'var(--c3)',
}
