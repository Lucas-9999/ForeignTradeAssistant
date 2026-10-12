import { useMutation, useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query'
import { createLead, fetchLeads, fetchLeadCommunications, type LeadInput, type LeadsParams } from '@/services/leads'

/** 线索列表（服务端分页）。keepPreviousData 翻页时保留上一页避免闪烁。 */
export function useLeads(params: LeadsParams) {
  return useQuery({
    queryKey: ['leads', params],
    queryFn: ({ signal }) => fetchLeads(params, signal),
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  })
}

/** 手工新增一条 new/manual 线索后，刷新全部线索查询缓存。 */
export function useCreateLead() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (data: LeadInput) => createLead(data),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['leads'] }),
  })
}

/** 单个线索的沟通时间线（详情抽屉用）。 */
export function useLeadCommunications(leadId: number | null) {
  return useQuery({
    queryKey: ['leads', leadId, 'communications'],
    queryFn: ({ signal }) => fetchLeadCommunications(leadId as number, signal),
    enabled: leadId != null,
    staleTime: 60_000,
  })
}
