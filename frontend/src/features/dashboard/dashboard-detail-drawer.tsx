import { ChevronLeft, ChevronRight, Inbox, Loader2 } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Drawer } from '@/components/ui/drawer'
import { useDashboardDetails } from '@/hooks/use-dashboard'
import { formatDateTime } from '@/lib/utils'
import { sourceZh } from '@/lib/zh'
import type { DashboardDetailItem, DashboardMetric } from '@/types'

const CLASS_ZH: Record<string, string> = {
  reply: '真实回复',
  bounce: '退信',
  auto_reply: '自动回复',
  unknown: '系统/未知',
}

export function DashboardDetailDrawer({
  metric,
  page,
  onPageChange,
  onClose,
}: {
  metric: DashboardMetric | null
  page: number
  onPageChange: (page: number) => void
  onClose: () => void
}) {
  const query = useDashboardDetails(metric, page)
  const data = query.data

  return (
    <Drawer open={metric !== null} onClose={onClose} title={data?.title ?? '指标明细'}>
      {query.isLoading ? (
        <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />正在加载明细
        </div>
      ) : query.isError ? (
        <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
          {query.error instanceof Error ? query.error.message : '明细加载失败'}
        </div>
      ) : data?.items.length ? (
        <div className="space-y-4">
          <div className="text-sm text-muted-foreground">
            共 {data.total.toLocaleString('zh-CN')} 条 · 第 {data.page}/{data.total_pages} 页
          </div>
          {data.kpi_total !== data.total ? (
            <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs leading-5 text-amber-900">
              指标总数为 {data.kpi_total.toLocaleString('zh-CN')}，其中可追溯到通信日志的明细为{' '}
              {data.total.toLocaleString('zh-CN')} 条。历史发送记录可能只写入了邮箱配额，未保存逐封通信明细。
            </div>
          ) : null}
          <div className="divide-y rounded-lg border">
            {data.items.map((item) => <DetailRow key={`${item.kind}-${item.id}`} item={item} />)}
          </div>
          <div className="flex items-center justify-between">
            <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => onPageChange(page - 1)}>
              <ChevronLeft className="h-4 w-4" />上一页
            </Button>
            <Button variant="outline" size="sm" disabled={page >= data.total_pages} onClick={() => onPageChange(page + 1)}>
              下一页<ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex h-48 flex-col items-center justify-center text-muted-foreground">
          <Inbox className="mb-2 h-7 w-7" />
          <p className="text-sm">当前指标没有可显示的明细</p>
          {data && data.kpi_total > 0 ? (
            <p className="mt-2 max-w-sm text-center text-xs leading-5">
              指标总数为 {data.kpi_total.toLocaleString('zh-CN')}，但历史记录未保存逐条通信明细。
            </p>
          ) : null}
        </div>
      )}
    </Drawer>
  )
}

function DetailRow({ item }: { item: DashboardDetailItem }) {
  const title = item.company_name || item.subject || item.email || `记录 #${item.id}`
  return (
    <div className="space-y-1.5 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 font-medium">{title}</div>
        {item.classification ? (
          <Badge variant={item.classification === 'bounce' ? 'destructive' : 'outline'}>
            {CLASS_ZH[item.classification] ?? item.classification}
          </Badge>
        ) : null}
      </div>
      {item.subject && item.company_name ? <div className="text-sm text-foreground/80">{item.subject}</div> : null}
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
        {item.email ? <span>{item.email}</span> : null}
        {item.country ? <span>{item.country}</span> : null}
        {item.source ? <span>{sourceZh(item.source)}</span> : null}
        {item.status ? <span>{item.status}</span> : null}
        {item.occurred_at ? <span>{formatDateTime(item.occurred_at)}</span> : null}
      </div>
    </div>
  )
}
