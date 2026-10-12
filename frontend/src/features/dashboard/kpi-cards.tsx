import { Users, Send, MessageSquare, AlertTriangle, Building2, MailCheck, Reply } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { formatCount } from '@/lib/utils'
import type { DashboardKpis, DashboardMetric } from '@/types'

interface KpiDef {
  label: string
  key: keyof DashboardKpis
  icon: typeof Users
  hint?: string
  accent?: string
}

const KPIS: KpiDef[] = [
  { label: '今日新增线索', key: 'today_new_leads', icon: Users, hint: '今日获取线索数' },
  { label: '今日发送邮件', key: 'today_emails_sent', icon: Send, hint: 'NetEase + Zoho 合计' },
  { label: '今日收到回复', key: 'today_replies', icon: MessageSquare, hint: '真实回复会话' },
  { label: '今日退信', key: 'today_bounces', icon: AlertTriangle, hint: '含邮箱退信通知', accent: 'text-red-500' },
  { label: '线索总数', key: 'total_leads', icon: Users, hint: '全部线索' },
  { label: '客户总数', key: 'total_customers', icon: Building2, hint: '已转化客户' },
  { label: '已联系线索', key: 'contacted_leads', icon: MailCheck, hint: '发过首封' },
  { label: '真实回复会话', key: 'replied_leads', icon: Reply, hint: '外联开始后按线程去重' },
]

export function KpiCards({
  kpis,
  onSelect,
}: {
  kpis: DashboardKpis | undefined
  onSelect?: (metric: DashboardMetric) => void
}) {
  if (!kpis) {
    return (
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        {Array.from({ length: 8 }).map((_, i) => (
          <Skeleton key={i} className="h-24 rounded-xl" />
        ))}
      </div>
    )
  }
  return (
    <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
      {KPIS.map((k) => {
        const Icon = k.icon
        return (
          <button
            key={k.key}
            type="button"
            className="rounded-xl text-left outline-none transition-transform hover:-translate-y-0.5 focus-visible:ring-2 focus-visible:ring-ring"
            onClick={() => onSelect?.(k.key)}
            aria-label={`查看${k.label}明细`}
          >
            <Card className="h-full transition-colors hover:border-primary/40 hover:bg-accent/20">
              <CardContent className="flex items-center gap-4 p-4">
                <div className="rounded-lg bg-primary/10 p-2.5">
                  <Icon className={`h-5 w-5 ${k.accent ?? 'text-primary'}`} />
                </div>
                <div className="min-w-0">
                  <div className="truncate text-xs text-muted-foreground">{k.label}</div>
                  <div className="text-2xl font-semibold tabular-nums">
                    {formatCount(kpis[k.key])}
                  </div>
                  <div className="truncate text-[11px] text-muted-foreground/70">{k.hint} · 点击查看</div>
                </div>
              </CardContent>
            </Card>
          </button>
        )
      })}
    </div>
  )
}
