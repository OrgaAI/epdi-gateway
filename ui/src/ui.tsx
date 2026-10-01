// Presentational primitives shared by every widget.

import type { ReactNode } from 'react'
import type { AlertLevel } from './types'

export function Card({
  title,
  note,
  actions,
  flush,
  children,
  className,
}: {
  title?: string
  note?: ReactNode
  actions?: ReactNode
  flush?: boolean
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`card${className ? ` ${className}` : ''}`}>
      {(title || actions || note) && (
        <header className="card-head">
          {title && <h2 className="card-title">{title}</h2>}
          {actions}
          {note && <div className="card-note">{note}</div>}
        </header>
      )}
      <div className={`card-body${flush ? ' flush' : ''}`}>{children}</div>
    </section>
  )
}

/**
 * Empty state that says WHY, not just "no data".
 *
 * The difference matters more here than in most dashboards: "no requests in this
 * range" and "the rate table is empty so every amount is zero" look identical on a
 * chart, and only one of them is a problem someone has to go and fix.
 */
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      {children && <p>{children}</p>}
    </div>
  )
}

export function Skeleton({ height = 18, width }: { height?: number | string; width?: number | string }) {
  return <div className="skeleton" style={{ height, width: width ?? '100%' }} aria-hidden="true" />
}

export function CardSkeleton({ height = 260, title }: { height?: number; title?: string }) {
  return (
    <Card title={title}>
      <Skeleton height={height} />
    </Card>
  )
}

export function Pill({
  level,
  children,
}: {
  level: AlertLevel | 'ok' | 'muted'
  children: ReactNode
}) {
  return <span className={`pill pill-${level}`}>{children}</span>
}

const METER_COLOURS: Record<string, string> = {
  ok: 'var(--ok)',
  notice: 'var(--notice)',
  warning: 'var(--warning)',
  critical: 'var(--critical)',
}

/**
 * Budget tiers: 75% and 90%, the same two thresholds the user notifications use.
 *
 * Must stay in step with the `alerts` computation in app/admin.py. Kept as one
 * function so the meter, the status pill and the alert panel cannot each decide
 * separately what counts as "close to the limit".
 */
export function budgetLevel(percent: number): AlertLevel | 'ok' {
  if (percent >= 90) return 'critical'
  if (percent >= 75) return 'warning'
  return 'ok'
}

/**
 * Budget meter for a table cell.
 *
 * The bar is capped at 100% while the printed number is not: someone at 140% of
 * budget must read as over the limit, and a bar that overflows its track just looks
 * like a rendering bug.
 */
export function Meter({ percent, label }: { percent: number; label: string }) {
  const level = budgetLevel(percent)
  return (
    <div className="meter">
      <div
        className="meter-track"
        role="progressbar"
        aria-valuenow={Math.round(percent)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`Budget used: ${label}`}
      >
        <div
          className="meter-fill"
          style={{ width: `${Math.min(100, Math.max(0, percent))}%`, background: METER_COLOURS[level] }}
        />
      </div>
      <span className="meter-value num">{label}</span>
    </div>
  )
}

export function Legend({
  items,
}: {
  items: Array<{ key: string; label: string; color: string; value?: string }>
}) {
  return (
    <div className="legend">
      {items.map((item) => (
        <span className="legend-item" key={item.key} title={item.key}>
          <span className="dot" style={{ background: item.color }} />
          <span className="legend-name">{item.label}</span>
          {item.value && <span className="num">{item.value}</span>}
        </span>
      ))}
    </div>
  )
}

export function Banner({
  level,
  title,
  children,
}: {
  level: 'warning' | 'critical'
  title: string
  children: ReactNode
}) {
  return (
    <div className={`banner banner-${level}`} role={level === 'critical' ? 'alert' : 'status'}>
      <span className="dot" style={{ background: METER_COLOURS[level], marginTop: 6 }} />
      <div>
        <strong>{title}</strong>
        <p>{children}</p>
      </div>
    </div>
  )
}

/** Shared tooltip body, so every chart explains itself the same way. */
export function ChartTooltip({
  title,
  rows,
  total,
}: {
  title: string
  rows: Array<{ key: string; label: string; color?: string; value: string }>
  total?: { label: string; value: string }
}) {
  return (
    <div className="tip">
      <div className="tip-title">{title}</div>
      {rows.map((row) => (
        <div className="tip-row" key={row.key}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7, minWidth: 0 }}>
            {row.color && <span className="dot" style={{ background: row.color }} />}
            <span className="legend-name">{row.label}</span>
          </span>
          <span>{row.value}</span>
        </div>
      ))}
      {total && (
        <div className="tip-row tip-total">
          <span>{total.label}</span>
          <span>{total.value}</span>
        </div>
      )}
    </div>
  )
}
