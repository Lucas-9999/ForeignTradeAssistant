import { get } from '@/services/api-client'
import type { DomainHealthResponse } from '@/types'

/** 只读取 P5 已保存快照；后端不会在此请求中执行 DNS 探测。 */
export function fetchDomainHealth(signal?: AbortSignal) {
  return get<DomainHealthResponse>('/api/v1/reputation/domain-health', undefined, { signal })
}
