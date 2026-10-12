import { useQuery } from '@tanstack/react-query'
import { fetchDomainHealth } from '@/services/reputation'

/** P8 信誉展示：5 分钟刷新一次已持久化的 P5 域健康快照。 */
export function useDomainHealth() {
  return useQuery({
    queryKey: ['reputation', 'domain-health'],
    queryFn: ({ signal }) => fetchDomainHealth(signal),
    staleTime: 60_000,
    refetchInterval: 300_000,
  })
}
