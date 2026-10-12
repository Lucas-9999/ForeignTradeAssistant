import { get, post } from './api-client'
import type { LeadItem, LeadsPage } from '@/types'

export interface LeadInput {
  company_name: string
  email: string
  contact_name?: string
  country?: string
  industry?: string
  notes?: string
  source: 'manual'
}

export interface LeadsParams {
  page?: number
  page_size?: number
  search?: string
  country?: string
  source?: string
  status?: string
  tag?: string
  has_sent?: boolean
  has_replied?: boolean
  sort?: string
}

export interface Communication {
  id: number
  lead_id: number | null
  channel: string | null
  direction: string
  subject: string | null
  body: string | null
  status: string | null
  error_message: string | null
  sent_at: string | null
  delivered_at: string | null
  replied_at: string | null
}

export function fetchLeads(params: LeadsParams, signal?: AbortSignal): Promise<LeadsPage> {
  return get<LeadsPage>('/api/v1/leads', { ...params }, { signal })
}

export function createLead(data: LeadInput): Promise<LeadItem> {
  return post<LeadItem>('/api/v1/leads', data)
}

export function fetchLeadCommunications(leadId: number, signal?: AbortSignal): Promise<{ lead_id: number; communications: Communication[] }> {
  return get<{ lead_id: number; communications: Communication[] }>(
    `/api/v1/leads/${leadId}/communications`,
    undefined,
    { signal },
  )
}
