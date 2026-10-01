// Mirrors the JSON that app/admin.py returns. Kept as one file so a change to the
// API surfaces here as a type error rather than as an empty widget at runtime.

export interface Totals {
  requests: number
  errors: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  total_tokens: number
  cost: number
  cache_hit_rate: number
  avg_latency_ms: number
  avg_ttfb_ms: number
  /** Records whose token counts are known to be partial (abandoned streams). */
  incomplete: number
  /** Records priced at zero because the model had no entry in the rate table. */
  unpriced: number
  error_rate: number
}

export interface UserRow extends Totals {
  sub: string
  username: string
  email: string
  budget: number
  budget_percent: number
}

export interface ModelRow extends Totals {
  model_id: string
}

export interface TrendPoint {
  date: string
  cost: number
  requests: number
  total_tokens: number
  by_model: Record<string, number>
}

export interface TokenTypeSlice {
  type: 'input' | 'output' | 'cache_read' | 'cache_write'
  tokens: number
}

export type AlertLevel = 'notice' | 'warning' | 'critical'

export interface BudgetAlert {
  sub: string
  username: string
  percent: number
  cost: number
  budget: number
  level: AlertLevel
}

export interface PricingInfo {
  version: string
  currency: string
  configured: boolean
  models: string[]
}

export interface MeteringInfo {
  enabled: boolean
  log_group: string
  stream: string
  table: string
  retention_days: number
  dropped_records: number
  budgets: {
    daily: number
    monthly: number
    active: boolean
    /** False when budgets are set but no rates are: nothing can actually trigger. */
    enforceable: boolean
  }
  pricing: PricingInfo
}

export interface Overview {
  meta: {
    generated_at: string
    range: { from: string; to: string; days: number }
    /** True when at least one day's query failed; totals are an under-count. */
    partial: boolean
    budgets: { daily: number; monthly: number }
    metering: MeteringInfo
  }
  summary: Totals & {
    active_users: number
    models_used: number
    avg_cost_per_request: number
  }
  users: UserRow[]
  models: ModelRow[]
  timeseries: TrendPoint[]
  token_types: TokenTypeSlice[]
  alerts: BudgetAlert[]
}

export interface UserDetail {
  user: { sub: string; username: string; email: string }
  summary: Totals
  daily: Array<Totals & { date: string }>
  models: ModelRow[]
  partial?: boolean
}

export interface Viewer {
  sub: string
  username: string
  email: string
  groups: string[]
}

export interface Meta {
  viewer: Viewer
  metering: MeteringInfo
  max_range_days: number
}
