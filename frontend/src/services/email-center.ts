import { get, post } from './api-client'
import type {
  EmailCenterAuditResponse,
  EmailCenterJob,
  EmailCenterOptions,
  EmailCenterPreview,
  EmailCenterPreviewInput,
} from '@/types'

export function fetchEmailCenterOptions(signal?: AbortSignal): Promise<EmailCenterOptions> {
  return get('/api/v1/email-center/options', undefined, { signal })
}

export function previewEmailBatch(data: EmailCenterPreviewInput): Promise<EmailCenterPreview> {
  return post('/api/v1/email-center/preview', data, undefined, { timeoutMs: 30_000 })
}

export function startEmailBatch(
  confirmationId: string,
  confirmationText: string,
  authorizationCode: string,
): Promise<{ job_id: string; status: string }> {
  return post(
    '/api/v1/email-center/send',
    { confirmation_id: confirmationId, confirmation_text: confirmationText },
    undefined,
    {
      timeoutMs: 15_000,
      headers: { 'X-FTA-Send-Token': authorizationCode },
    },
  )
}

export function fetchEmailJob(jobId: string, signal?: AbortSignal): Promise<EmailCenterJob> {
  return get(`/api/v1/email-center/status/${encodeURIComponent(jobId)}`, undefined, { signal })
}

export function fetchEmailAudit(signal?: AbortSignal): Promise<EmailCenterAuditResponse> {
  return get('/api/v1/email-center/audit', { limit: 20 }, { signal })
}
