import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { api, ApiError } from './api'
import { ModelBreakdown, Sparkline, SpendTrend, TokenMix, type Metric } from './charts'
import {
  compact,
  count,
  duration,
  money,
  percent,
  timestamp,
} from './format'
import type { Overview } from './types'
import { UsersTable } from './UsersTable'
import { UserDrawer } from './UserDrawer'
import { Banner, Card, CardSkeleton, Empty, Pill, Skeleton } from './ui'

// --- range handling -----------------------------------------------------------

type PresetId = '7d' | '30d' | 'mtd' | '90d'

const PRESETS: Array<{ id: PresetId; label: string }> = [
  { id: '7d', label: '7 days' },
  { id: '30d', label: '30 days' },
  { id: 'mtd', label: 'This month' },
  { id: '90d', label: '90 days' },
]

function isoDate(date: Date): string {
  return date.toISOString().slice(0, 10)
}

/**
 * Ranges are computed in UTC, matching how the records are partitioned.
 *
 * Not a detail to gloss over: the counters are keyed on the UTC date the request
 * happened, so deriving "today" from the browser's local midnight would shift the
 * whole range by a day for anyone west of UTC and quietly show the wrong window.
 */
function resolvePreset(preset: PresetId): { from: string; to: string } {
  const now = new Date()
  const to = isoDate(now)

  if (preset === 'mtd') {
    const first = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1))
    return { from: isoDate(first), to }
  }

  const days = preset === '7d' ? 6 : preset === '30d' ? 29 : 89
  const start = new Date(now.getTime() - days * 86_400_000)
  return { from: isoDate(start), to }
}

// --- app ----------------------------------------------------------------------

export default function App() {
  const [preset, setPreset] = useState<PresetId>('30d')
  const [custom, setCustom] = useState<{ from: string; to: string } | null>(null)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [data, setData] = useState<Overview | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<string | null>(null)

  const range = useMemo(() => custom ?? resolvePreset(preset), [custom, preset])

  // Kept in a ref as well so the refresh interval does not need the range in its
  // dependency list - otherwise every range change tears down and recreates the
  // timer, and a fast clicker never gets a refresh at all.
  const rangeRef = useRef(range)
  rangeRef.current = range

  const load = useCallback(async (signal?: AbortSignal, silent = false) => {
    if (!silent) setLoading(true)
    try {
      const payload = await api.overview(rangeRef.current.from, rangeRef.current.to, signal)
      setData(payload)
      setError(null)
    } catch (err: unknown) {
      if (signal?.aborted) return
      // A background refresh that fails leaves the previous data on screen rather
      // than replacing a working dashboard with an error card.
      if (!silent) setError(err instanceof ApiError ? err : new ApiError(0, 'request failed'))
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load, range.from, range.to])

  useEffect(() => {
    if (!autoRefresh) return
    // 60 s matches the Firehose buffer interval: polling faster cannot surface
    // anything newer and only costs DynamoDB reads.
    const timer = window.setInterval(() => void load(undefined, true), 60_000)
    return () => window.clearInterval(timer)
  }, [autoRefresh, load])

  const currency = data?.meta.metering.pricing.currency ?? 'USD'
  const pricingConfigured = data?.meta.metering.pricing.configured ?? false
  const budgetsActive = data?.meta.metering.budgets.active ?? false

  // Without rates every amount is zero, so the whole dashboard switches to tokens
  // as its primary measure instead of presenting a page of $0.00.
  const metric: Metric = pricingConfigured ? 'cost' : 'tokens'

  const modelOrder = useMemo(() => (data?.models ?? []).map((row) => row.model_id), [data])

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">
            ORGA<span> AI</span>
          </span>
          <span className="brand-sub">Consumption</span>
        </div>

        <div className="seg" role="group" aria-label="Date range preset">
          {PRESETS.map((option) => (
            <button
              key={option.id}
              aria-pressed={!custom && preset === option.id}
              onClick={() => {
                setCustom(null)
                setPreset(option.id)
              }}
            >
              {option.label}
            </button>
          ))}
        </div>

        <input
          className="input"
          type="date"
          value={range.from}
          max={range.to}
          aria-label="From date"
          onChange={(event) => setCustom({ from: event.target.value, to: range.to })}
        />
        <input
          className="input"
          type="date"
          value={range.to}
          min={range.from}
          aria-label="To date"
          onChange={(event) => setCustom({ from: range.from, to: event.target.value })}
        />

        <button
          className="btn"
          onClick={() => setAutoRefresh((value) => !value)}
          aria-pressed={autoRefresh}
          title="Refresh every 60 seconds"
        >
          <span className="dot" style={{ background: autoRefresh ? 'var(--ok)' : 'var(--text-faint)' }} />
          Live
        </button>

        <button className="btn" onClick={() => void load()} disabled={loading}>
          {loading ? 'Loading\u2026' : 'Refresh'}
        </button>

        <a className="btn btn-primary" href={api.exportUrl(range.from, range.to)}>
          Export CSV
        </a>
      </header>

      <main className="content">
        {error && (
          <Banner level="critical" title="Could not load consumption data">
            {error.message}
            {error.hint ? ` \u2014 ${error.hint}` : ''}
          </Banner>
        )}

        {data?.meta.partial && (
          <Banner level="warning" title="Figures are incomplete">
            At least one day in this range could not be read from the counters table,
            so the totals below are an under-count. Refresh, and if it persists check
            the gateway task logs.
          </Banner>
        )}

        {data && !pricingConfigured && (
          <Banner level="warning" title="Rate table not configured — amounts are not available">
            Token counts are exact, but every model is priced at zero because{' '}
            <code>spend_pricing</code> is empty in the Terraform variables. Fill it in
            with the Bedrock rates for this region and bump{' '}
            <code>spend_pricing_version</code>; the stored token counts mean costs can
            be reconstructed for past requests afterwards.
          </Banner>
        )}

        {data && budgetsActive && !data.meta.metering.budgets.enforceable && (
          <Banner level="critical" title="Budget enforcement cannot trigger">
            Per-user budgets are configured, but with no rates every request costs zero,
            so the 100% block will never fire. Configure the rate table to make
            enforcement effective.
          </Banner>
        )}

        {data && data.meta.metering.dropped_records > 0 && (
          <Banner level="warning" title="Some records were dropped">
            The gateway discarded {count(data.meta.metering.dropped_records)} record(s)
            because the metering queue was full. Those requests happened but are not
            counted here.
          </Banner>
        )}

        {/* KPI row */}
        {loading && !data ? (
          <div className="grid grid-kpi">
            {[0, 1, 2, 3, 4, 5].map((index) => (
              <div className="card kpi" key={index}>
                <Skeleton height={64} />
              </div>
            ))}
          </div>
        ) : data ? (
          <div className="grid grid-kpi">
            <Kpi
              label={pricingConfigured ? 'Total cost' : 'Total tokens'}
              value={
                pricingConfigured
                  ? money(data.summary.cost, currency)
                  : compact(data.summary.total_tokens)
              }
              foot={`${count(data.summary.requests)} requests`}
              spark={data.timeseries.map((point) =>
                pricingConfigured ? point.cost : point.total_tokens,
              )}
            />
            <Kpi
              label="Active technicians"
              value={count(data.summary.active_users)}
              foot={`${count(data.summary.models_used)} model(s) used`}
            />
            <Kpi
              label="Avg per request"
              value={
                pricingConfigured
                  ? money(data.summary.avg_cost_per_request, currency)
                  : compact(
                      data.summary.requests
                        ? data.summary.total_tokens / data.summary.requests
                        : 0,
                    )
              }
              foot={pricingConfigured ? 'cost per call' : 'tokens per call'}
            />
            <Kpi
              label="Cache hit rate"
              value={percent(data.summary.cache_hit_rate, 0)}
              foot={
                data.summary.cache_hit_rate >= 12
                  ? 'above break-even'
                  : 'below break-even (~12%)'
              }
              spark={data.timeseries.map((point) => point.requests)}
            />
            <Kpi
              label="Avg latency"
              value={duration(data.summary.avg_latency_ms)}
              foot={`first byte ${duration(data.summary.avg_ttfb_ms)}`}
            />
            <Kpi
              label="Errors"
              value={count(data.summary.errors)}
              foot={`${percent(data.summary.error_rate, 1)} of requests`}
            />
          </div>
        ) : null}

        {/* Budget alerts */}
        {data && data.alerts.length > 0 && (
          <Card
            title="Budget thresholds crossed"
            note={`${data.alerts.length} technician(s)`}
          >
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
              {data.alerts.map((alert) => (
                <button
                  key={alert.sub}
                  className="btn"
                  onClick={() => setSelected(alert.sub)}
                  style={{ gap: 10 }}
                >
                  <Pill level={alert.level}>{percent(alert.percent, 0)}</Pill>
                  <span>{alert.username}</span>
                  <span className="num" style={{ color: 'var(--text-muted)' }}>
                    {money(alert.cost, currency)} / {money(alert.budget, currency)}
                  </span>
                </button>
              ))}
            </div>
          </Card>
        )}

        {/* Trend + token mix */}
        <div className="grid grid-main">
          {loading && !data ? (
            <CardSkeleton title="Consumption over time" height={268} />
          ) : data ? (
            <Card
              title={pricingConfigured ? 'Spend over time, by model' : 'Tokens over time'}
              note={`${data.meta.range.days} day(s) \u00b7 UTC`}
              flush
            >
              <div style={{ padding: '14px 8px 0' }}>
                <SpendTrend
                  data={data.timeseries}
                  models={modelOrder}
                  metric={metric}
                  currency={currency}
                />
              </div>
            </Card>
          ) : null}

          {loading && !data ? (
            <CardSkeleton title="Token mix" height={240} />
          ) : data ? (
            <Card title="Token mix" note="by type" flush>
              <div style={{ padding: '14px 8px 0' }}>
                <TokenMix slices={data.token_types} />
              </div>
            </Card>
          ) : null}
        </div>

        {/* Models */}
        {data && (
          <div className="grid grid-split">
            <Card
              title={pricingConfigured ? 'Cost by model' : 'Tokens by model'}
              note="highest first"
            >
              <ModelBreakdown models={data.models} metric={metric} currency={currency} />
            </Card>

            <Card title="Cache efficiency by model" note="read share of input">
              {data.models.length === 0 ? (
                <Empty title="No model activity yet" />
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  {data.models.map((model) => (
                    <div key={model.model_id}>
                      <div
                        style={{
                          display: 'flex',
                          justifyContent: 'space-between',
                          fontSize: 12.5,
                          marginBottom: 5,
                        }}
                      >
                        <span title={model.model_id}>{model.model_id.split('.').pop()}</span>
                        <span className="num" style={{ color: 'var(--text-muted)' }}>
                          {percent(model.cache_hit_rate, 0)}
                        </span>
                      </div>
                      <div className="meter-track">
                        <div
                          className="meter-fill"
                          style={{
                            width: `${Math.min(100, model.cache_hit_rate)}%`,
                            background:
                              model.cache_hit_rate >= 50
                                ? 'var(--ok)'
                                : model.cache_hit_rate >= 12
                                  ? 'var(--notice)'
                                  : 'var(--warning)',
                          }}
                        />
                      </div>
                    </div>
                  ))}
                  <p style={{ margin: 0, color: 'var(--text-faint)', fontSize: 12 }}>
                    A cache write costs more than uncached input, so below roughly 12%
                    caching is a net loss. Claude Code caches aggressively, which is why
                    this is worth watching per model.
                  </p>
                </div>
              )}
            </Card>
          </div>
        )}

        {/* Users */}
        {loading && !data ? (
          <CardSkeleton title="Consumption by technician" height={300} />
        ) : data ? (
          <Card
            title="Consumption by technician"
            note={`${data.users.length} in range \u00b7 click a row for detail`}
            flush
          >
            <UsersTable
              users={data.users}
              currency={currency}
              budgetsActive={budgetsActive}
              onSelect={setSelected}
            />
          </Card>
        ) : null}

        {data && (
          <footer className="foot">
            <span>Updated {timestamp(data.meta.generated_at)} UTC</span>
            <span>
              Rates: {data.meta.metering.pricing.version} ({currency})
            </span>
            <span>Retention: {data.meta.metering.retention_days} days</span>
            {data.summary.incomplete > 0 && (
              <span>{count(data.summary.incomplete)} record(s) with partial token counts</span>
            )}
            <span>Metadata only &mdash; no prompt or response content is stored.</span>
          </footer>
        )}
      </main>

      {selected && (
        <UserDrawer
          sub={selected}
          from={range.from}
          to={range.to}
          currency={currency}
          metric={metric}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  )
}

function Kpi({
  label,
  value,
  foot,
  spark,
}: {
  label: string
  value: string
  foot?: string
  spark?: number[]
}) {
  return (
    <div className="card kpi">
      <div className="kpi-label">{label}</div>
      <div className="kpi-value num">{value}</div>
      {foot && <div className="kpi-foot">{foot}</div>}
      {spark && <Sparkline values={spark} />}
    </div>
  )
}
