import { Activity, RefreshCw, ShieldCheck } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useDomainHealth } from '@/hooks/use-reputation'
import type { DnsStatus, DomainHealthSnapshot } from '@/types'

const DNS_COLUMNS: { key: keyof Pick<DomainHealthSnapshot,
  'mx_status' | 'spf_status' | 'dkim_status' | 'dmarc_status' | 'a_status' | 'ptr_status' | 'tls_status' | 'authentication_status'>; label: string }[] = [
  { key: 'mx_status', label: 'MX' },
  { key: 'spf_status', label: 'SPF' },
  { key: 'dkim_status', label: 'DKIM' },
  { key: 'dmarc_status', label: 'DMARC' },
  { key: 'a_status', label: 'A/AAAA' },
  { key: 'ptr_status', label: 'PTR' },
  { key: 'tls_status', label: 'TLS' },
  { key: 'authentication_status', label: '认证' },
]

function dnsVariant(status: DnsStatus) {
  if (status === 'PASS') return 'success' as const
  if (status === 'FAIL') return 'destructive' as const
  return 'outline' as const
}

function healthVariant(status: DomainHealthSnapshot['status']) {
  if (status === 'healthy') return 'success' as const
  if (status === 'paused' || status === 'disabled') return 'destructive' as const
  return 'outline' as const
}

function healthLabel(status: DomainHealthSnapshot['status']) {
  return {
    healthy: '健康', warning: '警告', restricted: '受限', paused: '暂停', warming: '预热中', disabled: '已停用',
  }[status]
}

function checkedAt(row: DomainHealthSnapshot) {
  return row.last_checked ?? row.updated_at ?? '未记录'
}

/** P8 信誉页：只显示 P5 已落库的 DNS/TLS/认证事实，不执行任何投递控制。 */
export function ReputationPage() {
  const { data, isLoading, isError, error, refetch, isFetching } = useDomainHealth()
  const rows = data?.domains ?? []

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">投递信誉</h1>
          <p className="text-sm text-muted-foreground">
            P5 已持久化的域名 DNS、TLS 与认证快照 · 仅供观察，不会触发审计或改变发送节流
          </p>
        </div>
        <button
          type="button"
          onClick={() => refetch()}
          className="inline-flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-accent disabled:opacity-60"
          disabled={isFetching}
        >
          <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} /> 刷新
        </button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base"><ShieldCheck className="h-4 w-4 text-primary" />域健康快照</CardTitle>
          <CardDescription>数据来源：P5 DNS 健康审计已保存记录；无记录不代表域名异常。</CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="space-y-3">
              {Array.from({ length: 3 }).map((_, index) => <Skeleton key={index} className="h-11 w-full" />)}
            </div>
          ) : isError ? (
            <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-5 text-sm">
              <p className="font-medium text-destructive">域健康数据加载失败</p>
              <p className="mt-1 text-muted-foreground">{error instanceof Error ? error.message : '请确认后端服务正常运行。'}</p>
            </div>
          ) : rows.length === 0 ? (
            <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
              <Activity className="mx-auto mb-2 h-5 w-5" />
              暂无已保存的域健康快照。运行 P5 健康检查并明确选择持久化后，数据会在这里显示。
            </div>
          ) : (
            <Table>
              <TableHeader><TableRow>
                <TableHead>域名</TableHead><TableHead>评分</TableHead><TableHead>状态</TableHead>
                {DNS_COLUMNS.map((column) => <TableHead key={column.key}>{column.label}</TableHead>)}
                <TableHead>发送 IP</TableHead><TableHead>检查时间</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {rows.map((row) => <TableRow key={row.domain}>
                  <TableCell className="font-medium">{row.domain}</TableCell>
                  <TableCell>{row.health_score.toFixed(0)}</TableCell>
                  <TableCell><Badge variant={healthVariant(row.status)}>{healthLabel(row.status)}</Badge></TableCell>
                  {DNS_COLUMNS.map((column) => <TableCell key={column.key}><Badge variant={dnsVariant(row[column.key])}>{row[column.key]}</Badge></TableCell>)}
                  <TableCell className="font-mono text-xs text-muted-foreground">{row.sending_ip ?? '—'}</TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{checkedAt(row)}</TableCell>
                </TableRow>)}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
