// Per-user consumption table: the answer to "who used how much".
//
// This is the widget the client asked the question with, so it is the one that has
// to survive scrutiny: sortable on every numeric column, searchable, exportable, and
// honest about which rows carry caveats.

import { useMemo, useState } from 'react'
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from '@tanstack/react-table'

import { compact, count, duration, modelLabel, money, percent } from './format'
import type { UserRow } from './types'
import { Empty, Meter, Pill, budgetLevel } from './ui'

const helper = createColumnHelper<UserRow>()

export function UsersTable({
  users,
  currency,
  budgetsActive,
  onSelect,
}: {
  users: UserRow[]
  currency: string
  budgetsActive: boolean
  onSelect: (sub: string) => void
}) {
  const [search, setSearch] = useState('')
  // Default sort is cost descending, because the first question is always "who is
  // spending the most". Falls back to tokens through the accessor when every cost is
  // zero, so the table is still ordered usefully before rates are configured.
  const [sorting, setSorting] = useState<SortingState>([{ id: 'cost', desc: true }])

  const columns = useMemo(
    () => [
      helper.accessor('username', {
        header: 'Technician',
        cell: (info) => {
          const row = info.row.original
          return (
            <div className="who">
              <span className="who-name">{row.username}</span>
              {row.email && row.email !== row.username && (
                <span className="who-mail">{row.email}</span>
              )}
            </div>
          )
        },
      }),
      helper.accessor('cost', {
        header: 'Cost',
        // Ties broken by token volume so the ordering stays meaningful while every
        // cost is still zero.
        sortingFn: (a, b) =>
          a.original.cost - b.original.cost || a.original.total_tokens - b.original.total_tokens,
        cell: (info) => {
          const row = info.row.original
          return (
            <span className="num">
              {money(info.getValue(), currency)}
              {/* An amount computed from an unpriced model is not a real zero, and
                  saying so in the cell is the only place a reader will notice. */}
              {row.unpriced > 0 && (
                <span title={`${row.unpriced} request(s) had no rate configured`}> *</span>
              )}
            </span>
          )
        },
      }),
      ...(budgetsActive
        ? [
            helper.accessor('budget_percent', {
              header: 'Budget',
              cell: (info) => <Meter percent={info.getValue()} label={percent(info.getValue(), 0)} />,
            }),
          ]
        : []),
      helper.accessor('requests', {
        header: 'Requests',
        cell: (info) => <span className="num">{count(info.getValue())}</span>,
      }),
      helper.accessor('input_tokens', {
        header: 'Input',
        cell: (info) => <span className="num">{compact(info.getValue())}</span>,
      }),
      helper.accessor('output_tokens', {
        header: 'Output',
        cell: (info) => <span className="num">{compact(info.getValue())}</span>,
      }),
      helper.accessor('cache_read_tokens', {
        header: 'Cache read',
        cell: (info) => <span className="num">{compact(info.getValue())}</span>,
      }),
      helper.accessor('cache_hit_rate', {
        header: 'Cache hit',
        cell: (info) => {
          const value = info.getValue()
          // 12% is the break-even: below it, cache writes cost more than the reads
          // save, so a low number is actionable rather than merely unimpressive.
          const level = value >= 50 ? 'ok' : value >= 12 ? 'notice' : 'muted'
          return <Pill level={level}>{percent(value, 0)}</Pill>
        },
      }),
      helper.accessor('avg_latency_ms', {
        header: 'Avg latency',
        cell: (info) => <span className="num">{duration(info.getValue())}</span>,
      }),
      helper.accessor('error_rate', {
        header: 'Errors',
        cell: (info) => {
          const row = info.row.original
          if (row.errors === 0) return <span style={{ color: 'var(--text-faint)' }}>&ndash;</span>
          return (
            <Pill level={info.getValue() >= 10 ? 'critical' : 'warning'}>
              {count(row.errors)} ({percent(info.getValue(), 0)})
            </Pill>
          )
        },
      }),
      // display(), not accessor(): "status" is derived from budget_percent rather
      // than being a field on the row, and faking an accessor key that does not
      // exist on UserRow only works by defeating the type checker.
      helper.display({
        id: 'status',
        header: 'Status',
        cell: (info) => {
          const row = info.row.original
          if (!budgetsActive) return <Pill level="muted">no budget</Pill>
          const level = budgetLevel(row.budget_percent)
          if (level === 'ok') return <Pill level="ok">ok</Pill>
          return <Pill level={level}>{percent(row.budget_percent, 0)}</Pill>
        },
      }),
    ],
    [currency, budgetsActive],
  )

  const table = useReactTable({
    data: users,
    columns,
    state: { sorting, globalFilter: search },
    onSortingChange: setSorting,
    onGlobalFilterChange: setSearch,
    // Filter across name and email only. Including every numeric column would make
    // typing "4" match half the table through a latency value.
    globalFilterFn: (row, _columnId, value) => {
      const needle = String(value).toLowerCase()
      const user = row.original
      return (
        user.username.toLowerCase().includes(needle) ||
        user.email.toLowerCase().includes(needle) ||
        user.sub.toLowerCase().includes(needle)
      )
    },
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  })

  if (users.length === 0) {
    return (
      <Empty title="No consumption recorded">
        Once a technician sends a request through the gateway, they appear here with
        their token counts and cost.
      </Empty>
    )
  }

  const rows = table.getRowModel().rows

  return (
    <>
      <div style={{ padding: '12px 18px', borderBottom: '1px solid var(--border)' }}>
        <input
          className="input"
          type="search"
          placeholder="Filter by name, email or subject id"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          style={{ width: 'min(340px, 100%)' }}
          aria-label="Filter technicians"
        />
      </div>

      <div className="table-wrap">
        <table className="data">
          <thead>
            {table.getHeaderGroups().map((group) => (
              <tr key={group.id}>
                {group.headers.map((header, index) => {
                  const sortable = header.column.getCanSort()
                  const direction = header.column.getIsSorted()
                  return (
                    <th
                      key={header.id}
                      className={`${sortable ? 'sortable ' : ''}${index === 0 ? '' : 'align-right'}`}
                      onClick={sortable ? header.column.getToggleSortingHandler() : undefined}
                      aria-sort={
                        direction === 'asc'
                          ? 'ascending'
                          : direction === 'desc'
                            ? 'descending'
                            : undefined
                      }
                    >
                      {flexRender(header.column.columnDef.header, header.getContext())}
                      {direction && <span className="arrow">{direction === 'asc' ? '\u2191' : '\u2193'}</span>}
                    </th>
                  )
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={columns.length} style={{ color: 'var(--text-muted)' }}>
                  No technician matches &ldquo;{search}&rdquo;.
                </td>
              </tr>
            )}
            {rows.map((row) => (
              <tr
                key={row.id}
                className="row-clickable"
                // Keyboard reachable: the row is the only route to the per-user
                // breakdown, so making it mouse-only would put a whole feature
                // behind a pointer.
                tabIndex={0}
                role="button"
                aria-label={`Open breakdown for ${row.original.username}`}
                onClick={() => onSelect(row.original.sub)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    onSelect(row.original.sub)
                  }
                }}
              >
                {row.getVisibleCells().map((cell, index) => (
                  <td key={cell.id} className={index === 0 ? '' : 'align-right'}>
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

/** Compact per-model list, reused in the drawer. */
export function ModelList({ models, currency }: { models: Array<{ model_id: string; cost: number; requests: number; total_tokens: number }>; currency: string }) {
  if (models.length === 0) return <Empty title="No models used" />

  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th>Model</th>
            <th className="align-right">Requests</th>
            <th className="align-right">Tokens</th>
            <th className="align-right">Cost</th>
          </tr>
        </thead>
        <tbody>
          {models.map((model) => (
            <tr key={model.model_id}>
              <td title={model.model_id}>{modelLabel(model.model_id)}</td>
              <td className="align-right num">{count(model.requests)}</td>
              <td className="align-right num">{compact(model.total_tokens)}</td>
              <td className="align-right num">{money(model.cost, currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
