import { useState } from 'react'
import { useDashboard } from '@/hooks/use-dashboard'
import type { DashboardMetric } from '@/types'
import { KpiCards } from './kpi-cards'
import { ChartsGrid } from './charts'
import { DashboardDetailDrawer } from './dashboard-detail-drawer'

/** 与后端 date('now') 一致的 UTC 日（今日口径，与反垃圾配额表同源）。 */
const todayUtc = new Date().toISOString().slice(0, 10)

export function DashboardPage() {
  const { data, isLoading, isError, error, refetch } = useDashboard()
  const [detailMetric, setDetailMetric] = useState<DashboardMetric | null>(null)
  const [detailPage, setDetailPage] = useState(1)

  const openDetails = (metric: DashboardMetric) => {
    setDetailMetric(metric)
    setDetailPage(1)
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">仪表盘</h1>
          <p className="text-sm text-muted-foreground">
            今日业务概览与 30 天趋势 · 今日 = {todayUtc}（UTC，周末无采集/发送计划）
          </p>
        </div>
        <button
          type="button"
          onClick={() => refetch()}
          className="rounded-md border px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-accent"
        >
          刷新
        </button>
      </div>

      {isError ? (
        <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-6 text-center">
          <p className="font-medium text-destructive">数据加载失败</p>
          <p className="mt-1 text-sm text-muted-foreground">
            {error instanceof Error ? error.message : '未知错误'} —— 请确认后端 :8000 已启动。
          </p>
          <button
            type="button"
            onClick={() => refetch()}
            className="mt-3 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground"
          >
            重试
          </button>
        </div>
      ) : (
        <>
          <KpiCards kpis={isLoading ? undefined : data?.kpis} onSelect={openDetails} />
          <ChartsGrid overview={data} />
        </>
      )}
      <DashboardDetailDrawer
        metric={detailMetric}
        page={detailPage}
        onPageChange={setDetailPage}
        onClose={() => setDetailMetric(null)}
      />
    </div>
  )
}
