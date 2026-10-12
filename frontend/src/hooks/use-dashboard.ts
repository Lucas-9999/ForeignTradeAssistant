import { useQuery } from '@tanstack/react-query'
import { fetchDashboard, fetchDashboardDetails } from '@/services/dashboard'
import type { DashboardMetric } from '@/types'

/** 仪表盘数据（30 天窗口）。 */
export function useDashboard() {
  return useQuery({
    queryKey: ['dashboard', 'overview'],
    queryFn: ({ signal }) => fetchDashboard(signal),
    staleTime: 60_000,
    refetchInterval: 300_000, // 5 min auto-refresh
  })
}


export function useDashboardDetails(metric: DashboardMetric | null, page: number) {
  return useQuery({
    queryKey: ['dashboard', 'details', metric, page],
    queryFn: ({ signal }) => fetchDashboardDetails(metric!, page, signal),
    enabled: metric !== null,
    staleTime: 30_000,
  })
}
