import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  fetchEmailAudit,
  fetchEmailCenterOptions,
  fetchEmailJob,
  previewEmailBatch,
  startEmailBatch,
} from '@/services/email-center'
import type { EmailCenterPreviewInput } from '@/types'

export function useEmailCenterOptions() {
  return useQuery({
    queryKey: ['email-center', 'options'],
    queryFn: ({ signal }) => fetchEmailCenterOptions(signal),
    staleTime: 30_000,
  })
}

export function usePreviewEmailBatch() {
  return useMutation({ mutationFn: (data: EmailCenterPreviewInput) => previewEmailBatch(data) })
}

export function useStartEmailBatch() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({
      confirmationId,
      confirmationText,
      authorizationCode,
    }: {
      confirmationId: string
      confirmationText: string
      authorizationCode: string
    }) => startEmailBatch(confirmationId, confirmationText, authorizationCode),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['email-center', 'audit'] })
      qc.invalidateQueries({ queryKey: ['mailboxes'] })
      qc.invalidateQueries({ queryKey: ['dashboard'] })
    },
  })
}

export function useEmailJob(jobId: string | null) {
  return useQuery({
    queryKey: ['email-center', 'job', jobId],
    queryFn: ({ signal }) => fetchEmailJob(jobId as string, signal),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status === 'completed' || status === 'failed' ? false : 1_500
    },
  })
}

export function useEmailAudit() {
  return useQuery({
    queryKey: ['email-center', 'audit'],
    queryFn: ({ signal }) => fetchEmailAudit(signal),
    staleTime: 10_000,
  })
}
