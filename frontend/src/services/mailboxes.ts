/** Mailboxes API client — M1 read + M5 write (CRUD + SMTP test). */

import { get, post, put, del } from './api-client'
import type { Mailbox } from '@/types'

export interface MailboxInput {
  name: string
  display_name?: string
  smtp_host: string
  smtp_port: number
  use_tls: boolean
  username: string
  password?: string
  from_email?: string
  from_name?: string
  daily_limit?: number
}

/** 编辑可只传要改的字段；password 留空字符串 = 清除已存密码（回退 env）。 */
export type MailboxPatch = Partial<
  Pick<
    MailboxInput,
    'display_name' | 'smtp_host' | 'smtp_port' | 'use_tls' | 'username' | 'password' | 'from_email' | 'from_name' | 'daily_limit'
  >
> & { is_active?: number }

export interface MailboxTestResult {
  ok: boolean
  message: string
}

export function fetchMailboxes(signal?: AbortSignal): Promise<{ mailboxes: Mailbox[] }> {
  return get<{ mailboxes: Mailbox[] }>('/api/v1/mailboxes', undefined, { signal })
}

export function createMailbox(data: MailboxInput): Promise<Mailbox> {
  return post<Mailbox>('/api/v1/mailboxes', data)
}

export function updateMailbox(name: string, data: MailboxPatch): Promise<Mailbox> {
  return put<Mailbox>(`/api/v1/mailboxes/${encodeURIComponent(name)}`, data)
}

export function deleteMailbox(name: string): Promise<{ deleted: boolean; name: string }> {
  return del<{ deleted: boolean; name: string }>(`/api/v1/mailboxes/${encodeURIComponent(name)}`)
}

/** SMTP 连接测试最坏可能等到服务器超时 → 独立 20s 超时（默认 15s 太短）。 */
export function testMailbox(name: string): Promise<MailboxTestResult> {
  return post<MailboxTestResult>(
    `/api/v1/mailboxes/${encodeURIComponent(name)}/test`,
    undefined,
    undefined,
    { timeoutMs: 20_000 },
  )
}
