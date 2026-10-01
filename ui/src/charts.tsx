// Charts. All recharts, all fed from the single /admin/api/overview payload.
//
// Two conventions run through every chart here:
//
//   A model keeps its colour everywhere. The order used for colour assignment is
//   the payload's own (highest spend first), so the model responsible for the bill
//   is the same colour in the trend, the breakdown and the drawer. Charts that
//   recolour between widgets force the reader to re-learn the legend each time.
//
//   Tokens are the fallback metric, not an afterthought. Until the rate table is
//   filled in, every amount is zero - so each money chart can switch to token
//   counts rather than render a flat line at zero and look broken.

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import {
  colorFor,
  compact,
  longDate,
  modelLabel,
  money,
  percent as fmtPercent,
  shortDate,
  TOKEN_TYPE_COLORS,
  TOKEN_TYPE_LABELS,
} from './format'
import type { ModelRow, TokenTypeSlice, TrendPoint } from './types'
import { ChartTooltip, Empty, Legend } from './ui'

const AXIS = {
  stroke: 'var(--text-faint)',
  fontSize: 11,
  tickLine: false,
  axisLine: false,
} as const

export type Metric = 'cost' | 'tokens'

// --- spend / usage trend ------------------------------------------------------

export function SpendTrend({
  data,
  models,
  metric,
  currency,
}: {
  data: TrendPoint[]
  models: string[]
  metric: Metric
  currency: string
}) {
  const hasData = data.some((point) => point.requests > 0)
  if (!hasData) {
    return (
      <Empty title="No requests in this range">
        Nothing was sent through the gateway between these dates. Widen the range, or
        check that Claude Code is pointed at the gateway rather than at Bedrock
        directly.
      </Empty>
    )
  }

  // In cost mode the series are per model, so the stack shows WHICH model drove a
  // spike. In token mode there is one series: token counts per model are available
  // but the useful question without prices is volume over time, and a five-way
  // stack of token counts is harder to read than the total.
  const stacked = metric === 'cost'

  const rows = data.map((point) => {
    const row: Record<string, number | string> = { date: point.date }
    if (stacked) {
      for (const model of models) row[model] = point.by_model[model] ?? 0
    } else {
      row.total_tokens = point.total_tokens
    }
    row.requests = point.requests
    return row
  })

  return (
    <>
      <div style={{ height: 268 }}>
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={rows} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
            <defs>
              {/* A vertical fade rather than a flat fill: with five stacked series a
                  solid palette turns into a wall of colour and the boundaries between
                  bands stop being readable. */}
              {(stacked ? models : ['total_tokens']).map((key) => {
                const color = stacked ? colorFor(key, models) : 'var(--accent)'
                return (
                  <linearGradient id={`grad-${key}`} key={key} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={color} stopOpacity={0.55} />
                    <stop offset="100%" stopColor={color} stopOpacity={0.06} />
                  </linearGradient>
                )
              })}
            </defs>

            <XAxis dataKey="date" tickFormatter={shortDate} {...AXIS} minTickGap={24} />
            <YAxis
              {...AXIS}
              width={64}
              tickFormatter={(value: number) =>
                metric === 'cost' ? money(value, currency) : compact(value)
              }
            />
            <Tooltip
              cursor={{ stroke: 'var(--border-strong)' }}
              content={({ active, payload, label }) => {
                if (!active || !payload?.length) return null
                const total = payload.reduce((sum, item) => sum + (Number(item.value) || 0), 0)
                return (
                  <ChartTooltip
                    title={longDate(String(label))}
                    rows={payload
                      // Zero-value series are dropped: with a dozen models mapped, a
                      // tooltip listing ten "0" rows buries the two that matter.
                      .filter((item) => Number(item.value) > 0)
                      .map((item) => ({
                        key: String(item.dataKey),
                        label: stacked ? modelLabel(String(item.dataKey)) : 'Tokens',
                        color: item.color,
                        value:
                          metric === 'cost'
                            ? money(Number(item.value), currency)
                            : compact(Number(item.value)),
                      }))}
                    total={{
                      label: 'Total',
                      value: metric === 'cost' ? money(total, currency) : compact(total),
                    }}
                  />
                )
              }}
            />

            {stacked ? (
              models.map((model) => (
                <Area
                  key={model}
                  type="monotone"
                  dataKey={model}
                  stackId="spend"
                  stroke={colorFor(model, models)}
                  strokeWidth={1.5}
                  fill={`url(#grad-${model})`}
                />
              ))
            ) : (
              <Area
                type="monotone"
                dataKey="total_tokens"
                stroke="var(--accent)"
                strokeWidth={1.8}
                fill="url(#grad-total_tokens)"
              />
            )}
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {stacked && models.length > 0 && (
        <Legend
          items={models.map((model) => ({
            key: model,
            label: modelLabel(model),
            color: colorFor(model, models),
          }))}
        />
      )}
    </>
  )
}

// --- token type mix -----------------------------------------------------------

export function TokenMix({ slices }: { slices: TokenTypeSlice[] }) {
  const total = slices.reduce((sum, slice) => sum + slice.tokens, 0)

  if (total === 0) {
    return <Empty title="No tokens recorded">Token counts appear as soon as the first request completes.</Empty>
  }

  const data = slices.filter((slice) => slice.tokens > 0)

  return (
    <>
      <div style={{ height: 196, position: 'relative' }}>
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            {/* A donut, not a pie: the hole carries the total, which is the number
                people actually read first. */}
            <Pie
              data={data}
              dataKey="tokens"
              nameKey="type"
              innerRadius="58%"
              outerRadius="88%"
              paddingAngle={2}
              stroke="var(--surface)"
              strokeWidth={2}
            >
              {data.map((slice) => (
                <Cell key={slice.type} fill={TOKEN_TYPE_COLORS[slice.type]} />
              ))}
            </Pie>
            <Tooltip
              content={({ active, payload }) => {
                if (!active || !payload?.length) return null
                const slice = payload[0]?.payload as TokenTypeSlice | undefined
                if (!slice) return null
                return (
                  <ChartTooltip
                    title={TOKEN_TYPE_LABELS[slice.type] ?? slice.type}
                    rows={[
                      { key: 'tokens', label: 'Tokens', value: compact(slice.tokens) },
                      {
                        key: 'share',
                        label: 'Share',
                        value: fmtPercent((slice.tokens / total) * 100),
                      },
                    ]}
                  />
                )
              }}
            />
          </PieChart>
        </ResponsiveContainer>

        <div
          style={{
            position: 'absolute',
            inset: 0,
            display: 'grid',
            placeItems: 'center',
            pointerEvents: 'none',
          }}
        >
          <div style={{ textAlign: 'center' }}>
            <div className="num" style={{ fontSize: 22, fontWeight: 700 }}>
              {compact(total)}
            </div>
            <div style={{ color: 'var(--text-faint)', fontSize: 11.5 }}>tokens</div>
          </div>
        </div>
      </div>

      <Legend
        items={data.map((slice) => ({
          key: slice.type,
          label: TOKEN_TYPE_LABELS[slice.type] ?? slice.type,
          color: TOKEN_TYPE_COLORS[slice.type] ?? 'var(--c8)',
          value: fmtPercent((slice.tokens / total) * 100, 0),
        }))}
      />
    </>
  )
}

// --- per model ----------------------------------------------------------------

export function ModelBreakdown({
  models,
  metric,
  currency,
}: {
  models: ModelRow[]
  metric: Metric
  currency: string
}) {
  if (models.length === 0) {
    return <Empty title="No model activity">Per-model figures appear once requests are recorded.</Empty>
  }

  const order = models.map((row) => row.model_id)
  const data = models.map((row) => ({
    ...row,
    label: modelLabel(row.model_id),
    value: metric === 'cost' ? row.cost : row.total_tokens,
  }))

  return (
    <div style={{ height: Math.max(160, data.length * 38 + 24) }}>
      <ResponsiveContainer width="100%" height="100%">
        {/* Horizontal bars: model names are long, and rotated labels on a vertical
            bar chart are the single most common way this kind of panel becomes
            unreadable. */}
        <BarChart data={data} layout="vertical" margin={{ top: 4, right: 16, left: 4, bottom: 4 }}>
          <XAxis
            type="number"
            {...AXIS}
            tickFormatter={(value: number) =>
              metric === 'cost' ? money(value, currency) : compact(value)
            }
          />
          <YAxis type="category" dataKey="label" {...AXIS} width={118} />
          <Tooltip
            cursor={{ fill: 'rgba(255,255,255,0.04)' }}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null
              const row = payload[0]?.payload as (ModelRow & { label: string }) | undefined
              if (!row) return null
              return (
                <ChartTooltip
                  title={row.model_id}
                  rows={[
                    { key: 'cost', label: 'Cost', value: money(row.cost, currency) },
                    { key: 'req', label: 'Requests', value: compact(row.requests) },
                    { key: 'in', label: 'Input', value: compact(row.input_tokens) },
                    { key: 'out', label: 'Output', value: compact(row.output_tokens) },
                    { key: 'cache', label: 'Cache hit', value: fmtPercent(row.cache_hit_rate) },
                    { key: 'lat', label: 'Avg latency', value: `${row.avg_latency_ms} ms` },
                  ]}
                />
              )
            }}
          />
          <Bar dataKey="value" radius={[0, 4, 4, 0]} barSize={18}>
            {data.map((row) => (
              <Cell key={row.model_id} fill={colorFor(row.model_id, order)} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

// --- sparkline for KPI cards --------------------------------------------------

export function Sparkline({ values, color = 'var(--accent)' }: { values: number[]; color?: string }) {
  // A flat line at zero is noise, not information: it suggests a trend was measured
  // and found to be flat, when in fact there is nothing to show.
  if (values.length < 2 || values.every((value) => value === 0)) return null

  const data = values.map((value, index) => ({ index, value }))

  return (
    <div className="kpi-spark" aria-hidden="true">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 6, right: 0, left: 0, bottom: 0 }}>
          <Line type="monotone" dataKey="value" stroke={color} strokeWidth={1.6} dot={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

// --- daily bars for the user drawer -------------------------------------------

export function UserDaily({
  daily,
  metric,
  currency,
}: {
  daily: Array<{ date: string; cost: number; total_tokens: number; requests: number }>
  metric: Metric
  currency: string
}) {
  if (!daily.some((day) => day.requests > 0)) {
    return <Empty title="No activity in this range" />
  }

  return (
    <div style={{ height: 180 }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={daily} margin={{ top: 6, right: 6, left: 0, bottom: 0 }}>
          <XAxis dataKey="date" tickFormatter={shortDate} {...AXIS} minTickGap={20} />
          <YAxis
            {...AXIS}
            width={58}
            tickFormatter={(value: number) =>
              metric === 'cost' ? money(value, currency) : compact(value)
            }
          />
          <Tooltip
            cursor={{ fill: 'rgba(255,255,255,0.04)' }}
            content={({ active, payload, label }) => {
              if (!active || !payload?.length) return null
              const row = payload[0]?.payload as
                | { cost: number; total_tokens: number; requests: number }
                | undefined
              if (!row) return null
              return (
                <ChartTooltip
                  title={longDate(String(label))}
                  rows={[
                    { key: 'cost', label: 'Cost', value: money(row.cost, currency) },
                    { key: 'tokens', label: 'Tokens', value: compact(row.total_tokens) },
                    { key: 'req', label: 'Requests', value: compact(row.requests) },
                  ]}
                />
              )
            }}
          />
          <Bar
            dataKey={metric === 'cost' ? 'cost' : 'total_tokens'}
            fill="var(--accent)"
            radius={[3, 3, 0, 0]}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
