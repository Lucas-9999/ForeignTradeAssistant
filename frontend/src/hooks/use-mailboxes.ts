import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  fetchMailboxes,
  createMailbox,
  updateMailbox,
  deleteMailbox,
  testMailbox,
  type MailboxInput,
  type MailboxPatch,
} from '@/services/mailboxes'

/** 邮箱账号列表（M5 起支持新增任意 SMTP 账号 + 编辑/删除/测试连接）。 */
export function useMailboxes() {
  return useQuery({
    queryKey: ['mailboxes'],
    queryFn: ({ signal }) => fetchMailboxes(signal),
    staleTime: 30_000,
    refetchInterval: 300_000,
  })
}

/** 任一邮箱写操作后刷新列表（sent/remaining 配额态一并更新）。 */
function invalidateMailboxCaches(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: ['mailboxes'] })
  qc.invalidateQueries({ queryKey: ['dashboard'] })
}

export function useCreateMailbox() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data: MailboxInput) => createMailbox(data),
    onSuccess: () => invalidateMailboxCaches(qc),
  })
}

export function useUpdateMailbox() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ name, data }: { name: string; data: MailboxPatch }) => updateMailbox(name, data),
    onSuccess: () => invalidateMailboxCaches(qc),
  })
}

export function useDeleteMailbox() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (name: string) => deleteMailbox(name),
    onSuccess: () => invalidateMailboxCaches(qc),
  })
}

export function useTestMailbox() {
  return useMutation({
    mutationFn: (name: string) => testMailbox(name),
  })
}
