import type { Meta, Overview, UserDetail } from './types'

const BASE = '/admin/api'

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly hint?: string,
  ) {
    super(message)
  }
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    signal,
    // The ALB session lives in a cookie; without this the dashboard is
    // unauthenticated on every request and bounces through the login flow.
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  })

  if (!response.ok) {
    // A 401 here means the ALB session expired mid-session. Reloading restarts the
    // OIDC redirect and lands the user back on the page, which is far less
    // confusing than an error toast they cannot act on.
    if (response.status === 401) {
      window.location.reload()
      throw new ApiError(401, 'session expired, reloading')
    }

    let message = `${response.status} ${response.statusText}`
    let hint: string | undefined
    try {
      const body = await response.json()
      message = body.error ?? message
      hint = body.hint
    } catch {
      // Response was not JSON; the status line is the best available message.
    }
    throw new ApiError(response.status, message, hint)
  }

  return response.json() as Promise<T>
}

export const api = {
  meta: (signal?: AbortSignal) => get<Meta>('/meta', signal),

  overview: (from: string, to: string, signal?: AbortSignal) =>
    get<Overview>(`/overview?from=${from}&to=${to}`, signal),

  user: (sub: string, from: string, to: string, signal?: AbortSignal) =>
    get<UserDetail>(`/users/${encodeURIComponent(sub)}?from=${from}&to=${to}`, signal),

  // A plain link rather than a fetch: letting the browser handle the download means
  // the Content-Disposition filename is honoured and no blob has to be held in
  // memory.
  exportUrl: (from: string, to: string) => `${BASE}/export.csv?from=${from}&to=${to}`,
}
