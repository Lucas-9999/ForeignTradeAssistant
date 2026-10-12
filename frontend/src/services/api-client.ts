/** Minimal typed fetch client for the FTA backend (same-origin /ui/ or Vite proxy). */

export interface FieldError {
  field: string
  message: string
}

export class ApiError extends Error {
  status: number
  fieldErrors?: FieldError[]

  constructor(status: number, message: string, fieldErrors?: FieldError[]) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.fieldErrors = fieldErrors
  }
}

type QueryParams = Record<string, string | number | boolean | string[] | undefined>

export interface RequestOpts {
  /** Abort after this many ms. Default 15s; SMTP test overrides to 20s. */
  timeoutMs?: number
  /** External abort signal (React Query cancel) — merged with the internal timer. */
  signal?: AbortSignal
  /** Per-request headers (e.g. the in-memory M6 operator token). */
  headers?: Record<string, string>
}

const DEFAULT_TIMEOUT_MS = 15_000

/** Parse FastAPI error bodies: `{detail: string}` or `{detail: [...]}` (Pydantic 422). */
function parseErrorBody(b: unknown): { message: string; fieldErrors?: FieldError[] } {
  if (typeof b !== 'object' || b === null || !('detail' in b)) {
    return { message: String(b ?? '') || '请求失败' }
  }
  const detail = (b as { detail: unknown }).detail
  if (typeof detail === 'string') {
    return { message: detail }
  }
  if (Array.isArray(detail)) {
    const fieldErrors: FieldError[] = []
    let message = ''
    for (const item of detail) {
      if (typeof item !== 'object' || item === null) continue
      const loc = Array.isArray((item as { loc?: unknown[] }).loc) ? (item as { loc: unknown[] }).loc : []
      const field = String(loc[loc.length - 1] ?? '')
      const msg = String((item as { msg?: unknown }).msg ?? '')
      fieldErrors.push({ field, message: msg })
      if (!message && msg) message = `${field ? `${field}：` : ''}${msg}`
    }
    return { message: message || '请求参数有误', fieldErrors }
  }
  return { message: '请求失败' }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  params?: QueryParams,
  opts?: RequestOpts,
): Promise<T> {
  const url = new URL(path, window.location.origin)
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null) continue
      if (Array.isArray(value)) {
        for (const v of value) url.searchParams.append(key, String(v))
      } else {
        url.searchParams.set(key, String(value))
      }
    }
  }

  // 合并内部超时与外部 signal：任一触发即中止，finally 统一清理，避免泄漏。
  const controller = new AbortController()
  const timer = setTimeout(
    () => controller.abort(new DOMException('请求超时，请稍后重试', 'TimeoutError')),
    opts?.timeoutMs ?? DEFAULT_TIMEOUT_MS,
  )
  const onExternalAbort = () => controller.abort()
  opts?.signal?.addEventListener('abort', onExternalAbort)

  let res: Response
  try {
    res = await fetch(url.pathname + url.search, {
      method,
      headers: {
        Accept: 'application/json',
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...opts?.headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    })
  } finally {
    clearTimeout(timer)
    opts?.signal?.removeEventListener('abort', onExternalAbort)
  }

  if (!res.ok) {
    let message = res.statusText
    let fieldErrors: FieldError[] | undefined
    try {
      const parsed = parseErrorBody(await res.json())
      message = parsed.message || res.statusText
      fieldErrors = parsed.fieldErrors
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, message, fieldErrors)
  }
  return res.json() as Promise<T>
}

export function get<T>(path: string, params?: QueryParams, opts?: RequestOpts): Promise<T> {
  return request<T>('GET', path, undefined, params, opts)
}

export function post<T>(path: string, body?: unknown, params?: QueryParams, opts?: RequestOpts): Promise<T> {
  return request<T>('POST', path, body, params, opts)
}

export function put<T>(path: string, body?: unknown, params?: QueryParams, opts?: RequestOpts): Promise<T> {
  return request<T>('PUT', path, body, params, opts)
}

/** DELETE — 支持请求体（如批量移除 lead_tags）。 */
export function del<T>(path: string, body?: unknown, params?: QueryParams, opts?: RequestOpts): Promise<T> {
  return request<T>('DELETE', path, body, params, opts)
}
