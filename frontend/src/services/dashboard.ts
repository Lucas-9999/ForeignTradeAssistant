import { get } from './api-client'
import type { DashboardDetails, DashboardMetric, DashboardOverview } from '@/types'

export function fetchDashboard(signal?: AbortSignal): Promise<DashboardOverview> {
  return get<DashboardOverview>('/api/v1/dashboard/overview', undefined, { signal })
}

export function fetchDashboardDetails(
  metric: DashboardMetric,
  page: number,
  signal?: AbortSignal,
): Promise<DashboardDetails> {
  return get<DashboardDetails>(
    `/api/v1/dashboard/details/${metric}`,
    { page, page_size: 50 },
    { signal },
  )
}
