/** API 响应类型 —— 与后端 /api/v1/* 返回契约一一对应。 */

export interface DashboardOverview {
  kpis: DashboardKpis
  series_30d: {
    leads_created: SeriesPoint[]
    emails_sent: SeriesPoint[]
    replies: SeriesPoint[]
    bounces: SeriesPoint[]
  }
  distributions: {
    by_source: NameCount[]
    by_country: NameCount[]
    by_status: NameCount[]
    by_mailbox: NameCount[]
  }
}

export interface DashboardKpis {
  today_new_leads: number
  today_emails_sent: number
  today_replies: number
  today_bounces: number
  total_leads: number
  total_customers: number
  contacted_leads: number
  replied_leads: number
}

export type DashboardMetric = keyof DashboardKpis

export interface DashboardDetailItem {
  id: number
  kind: 'lead' | 'customer' | 'communication'
  lead_id: number | null
  company_name: string | null
  email: string | null
  country: string | null
  source: string | null
  status: string | null
  subject: string | null
  classification: '' | 'reply' | 'bounce' | 'auto_reply' | 'unknown'
  occurred_at: string | null
}

export interface DashboardDetails {
  metric: DashboardMetric
  title: string
  kpi_total: number
  total: number
  page: number
  page_size: number
  total_pages: number
  items: DashboardDetailItem[]
}

export interface SeriesPoint {
  day: string
  count: number
}

export interface NameCount {
  name: string
  count: number
}

export interface LeadItem {
  id: number
  company_name: string | null
  contact_name: string | null
  email: string | null
  phone: string | null
  whatsapp: string | null
  website: string | null
  source: string | null
  source_url: string | null
  industry: string | null
  country: string | null
  notes: string | null
  status: string
  duplicate_of: number | null
  discovered_at: string | null
  first_contacted_at: string | null
  last_contacted_at: string | null
  has_sent: boolean
  has_replied: boolean
  sent_count: number
  reply_count: number
  last_activity_at: string | null
  tags: { id: number; name: string; category: string; color: string }[]
  groups: { id: number; name: string; color: string }[]
}

export interface LeadsPage {
  items: LeadItem[]
  total: number
  page: number
  page_size: number
  total_pages: number
  total_by_status: Record<string, number>
}

export interface Mailbox {
  name: string
  display_name: string
  smtp_host: string
  smtp_port: number
  use_tls: boolean
  username: string
  from_email: string
  from_name: string
  has_smtp_password: boolean
  daily_limit: number
  sent_today: number
  remaining: number
  quota_table: string
  is_active: number
  source: string
  credential_source: 'encrypted' | 'env' | 'missing'
}

export interface EmailCenterMailboxOption {
  name: string
  display_name: string
  remaining: number
  daily_limit: number
  credential_source: 'encrypted' | 'env' | 'missing'
  has_smtp_password: boolean
}

export interface EmailCenterOptions {
  send_enabled: boolean
  max_batch_size: number
  mailboxes: EmailCenterMailboxOption[]
  template_categories: string[]
  templates: { id: string; name: string; category: string; language: string }[]
  country_groups: string[]
  industry_groups: string[]
}

export interface EmailCenterPreviewInput {
  mailbox: string
  limit: number
  country_filter?: string
  country_group?: string
  industry_group?: string
  include_low_relevance: boolean
  category?: string
  template_id?: string
  source?: string
  lead_ids?: number[]
}

export interface EmailCenterPreviewSample {
  lead_id: number
  company_name: string | null
  email: string
  subject: string
  body_html: string
  template_id: string | null
  template_name: string | null
}

export interface EmailCenterPreview {
  candidate_count: number
  lead_ids: number[]
  samples: EmailCenterPreviewSample[]
  confirmation_id: string | null
  confirmation_text: string | null
  expires_at: string | null
  message: string
}

export interface EmailCenterJob {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  mailbox: string
  requested: number
  sent: number
  skipped: number
  errors: number
  started_at: string
  completed_at: string | null
  error: string | null
}

export interface EmailCenterAuditItem {
  at: string
  event: string
  job_id?: string
  mailbox?: string
  requested?: number
  candidate_count?: number
  sent?: number
  skipped?: number
  errors?: number
}

export interface EmailCenterAuditResponse {
  items: EmailCenterAuditItem[]
}

/** P5 已持久化的域名健康快照；只读展示，不会触发 DNS 审计。 */
export interface DomainHealthSnapshot {
  domain: string
  health_score: number
  status: 'healthy' | 'warning' | 'restricted' | 'paused' | 'warming' | 'disabled'
  mx_status: DnsStatus
  spf_status: DnsStatus
  dkim_status: DnsStatus
  dmarc_status: DnsStatus
  a_status: DnsStatus
  ptr_status: DnsStatus
  tls_status: DnsStatus
  sending_ip: string | null
  authentication_status: DnsStatus
  last_checked: string | null
  updated_at: string | null
}

export type DnsStatus = 'PASS' | 'WARNING' | 'FAIL' | 'UNKNOWN'

export interface DomainHealthResponse {
  source: 'p5_persisted_snapshot'
  domains: DomainHealthSnapshot[]
}
