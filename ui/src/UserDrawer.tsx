// Per-technician drill-down, in a slide-over rather than a separate page.
//
// A drawer keeps the table underneath: the reader arrived from a row and almost
// always wants to go back and compare against a neighbour. A full page navigation
// loses the sort, the filter and the scroll position for no benefit.

import { useEffect, useRef, useState } from 'react'

import { api, ApiError } from './api'
import { UserDaily, type Metric } from './charts'
import { compact, count, duration, money, percent } from './format'
import { ModelList } from './UsersTable'
import type { UserDetail } from './types'
import { Card, Empty, Skeleton } from './ui'

export function UserDrawer({
  sub,
  from,
  to,
  currency,
  metric,
  onClose,
}: {
  sub: string
  from: string
  to: string
  currency: string
  metric: Metric
  onClose: () => void
}) {
  const [detail, setDetail] = useState<UserDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const closeRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const controller = new AbortController()
    setDetail(null)
    setError(null)

    api
      .user(sub, from, to, controller.signal)
      .then(setDetail)
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        setError(err instanceof ApiError ? err.message : 'could not load this technician')
      })

    return () => controller.abort()
  }, [sub, from, to])

  // Escape closes, and focus moves into the drawer on open. Without the focus move
  // a keyboard user is left tabbing through the table behind the overlay.
  useEffect(() => {
    closeRef.current?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="Technician breakdown">
        <header className="drawer-head">
          <div style={{ minWidth: 0 }}>
            <h2 className="drawer-title">{detail?.user.username ?? 'Loading\u2026'}</h2>
            {detail?.user.email && (
              <div style={{ color: 'var(--text-faint)', fontSize: 12.5 }}>{detail.user.email}</div>
            )}
          </div>
          <button ref={closeRef} className="btn" onClick={onClose} style={{ marginLeft: 'auto' }}>
            Close
          </button>
        </header>

        <div className="drawer-body">
          {error && (
            <Card title="Could not load">
              <Empty title={error} />
            </Card>
          )}

          {!detail && !error && (
            <>
              <Skeleton height={92} />
              <Skeleton height={200} />
              <Skeleton height={160} />
            </>
          )}

          {detail && (
            <>
              <div className="stat-list">
                <Stat label="Cost" value={money(detail.summary.cost, currency)} />
                <Stat label="Requests" value={count(detail.summary.requests)} />
                <Stat label="Tokens" value={compact(detail.summary.total_tokens)} />
                <Stat label="Cache hit" value={percent(detail.summary.cache_hit_rate, 0)} />
                <Stat label="Avg latency" value={duration(detail.summary.avg_latency_ms)} />
                <Stat label="Time to first byte" value={duration(detail.summary.avg_ttfb_ms)} />
              </div>

              <Card title="Daily" note={metric === 'cost' ? 'cost' : 'tokens'}>
                <UserDaily daily={detail.daily} metric={metric} currency={currency} />
              </Card>

              <Card title="By model" flush>
                <ModelList models={detail.models} currency={currency} />
              </Card>

              {(detail.summary.incomplete > 0 || detail.summary.unpriced > 0) && (
                <Card title="Caveats on these figures">
                  <ul style={{ margin: 0, paddingLeft: 18, color: 'var(--text-muted)', fontSize: 13 }}>
                    {detail.summary.incomplete > 0 && (
                      <li>
                        <strong>{count(detail.summary.incomplete)}</strong> request(s) had
                        incomplete token counts, because the client disconnected before
                        Bedrock sent its final metrics. Real consumption for those is
                        higher than shown.
                      </li>
                    )}
                    {detail.summary.unpriced > 0 && (
                      <li>
                        <strong>{count(detail.summary.unpriced)}</strong> request(s) ran on
                        a model with no configured rate, so they contribute tokens but no
                        cost.
                      </li>
                    )}
                  </ul>
                </Card>
              )}
            </>
          )}
        </div>
      </aside>
    </>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value num">{value}</div>
    </div>
  )
}
