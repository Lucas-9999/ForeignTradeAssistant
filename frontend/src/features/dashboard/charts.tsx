import {
  ResponsiveContainer,
  AreaChart,
  Area,
  BarChart,
  Bar,
  LineChart,
  Line,
  PieChart,
  Pie,
  Cell,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from 'recharts'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { sourceZh, MAILBOX_ZH } from '@/lib/zh'
import type { DashboardOverview, NameCount, SeriesPoint } from '@/types'

const PALETTE = ['#2563eb', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4', '#ec4899', '#84cc16']

function dayShort(day: string): string {
  return day.slice(5) // 'YYYY-MM-DD' → 'MM-DD'
}

function ChartShell({ title, desc, children }: { title: string; desc?: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        {desc ? <CardDescription>{desc}</CardDescription> : null}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

/**
 * Recharts 自定义 Tooltip。
 *
 * 注意：Recharts 把当前悬停点的值放在 `payload` 数组里（payload[i].name / .value / .color），
 * 而不是作为顶层 props 传给 content —— 之前 TooltipLabel 从顶层解构 name/value 恒为 undefined，
 * 导致所有图表 tooltip 都显示 0（柱状图本身高度正常）。这里统一从 payload 读取。
 */
interface TooltipPayloadItem {
  name?: string | number
  value?: string | number
  color?: string
  dataKey?: string | number
}

function ChartTooltip({
  active,
  payload,
  label,
  unit,
  noLabel,
}: {
  active?: boolean
  payload?: TooltipPayloadItem[]
  label?: string | number
  unit?: string
  noLabel?: boolean
}) {
  if (!active || !payload || payload.length === 0) return null
  return (
    <div className="rounded-md border bg-white px-3 py-2 text-xs shadow">
      {!noLabel && label != null && label !== '' ? <div className="font-medium">{label}</div> : null}
      <div className="space-y-0.5">
        {payload.map((p, i) => (
          <div key={i} className="flex items-center gap-2">
            {p.color ? (
              <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: p.color }} />
            ) : null}
            <span className="text-muted-foreground">
              {String(p.name ?? '')}:{' '}
              <span className="font-semibold text-foreground tabular-nums">
                {Number(p.value ?? 0).toLocaleString()}
                {unit ?? ''}
              </span>
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── 30-day series charts ──────────────────────────────────────────────────

export function LeadsCreatedChart({ data }: { data: SeriesPoint[] }) {
  return (
    <ChartShell title="30 天线索增长" desc="每日新增线索数量">
      <div className="h-52">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
            <defs>
              <linearGradient id="gLeads" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#2563eb" stopOpacity={0.35} />
                <stop offset="100%" stopColor="#2563eb" stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
            <XAxis dataKey="day" tickFormatter={dayShort} tick={{ fontSize: 10 }} interval="preserveStartEnd" />
            <YAxis tick={{ fontSize: 10 }} width={40} allowDecimals={false} />
            <Tooltip content={<ChartTooltip />} />
            <Area type="monotone" dataKey="count" name="新增线索" stroke="#2563eb" fill="url(#gLeads)" strokeWidth={2} />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </ChartShell>
  )
}

export function EmailsSentChart({ data }: { data: SeriesPoint[] }) {
  return (
    <ChartShell title="30 天发送量" desc="每日发出邮件（NetEase + Zoho）">
      <div className="h-52">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
            <XAxis dataKey="day" tickFormatter={dayShort} tick={{ fontSize: 10 }} interval="preserveStartEnd" />
            <YAxis tick={{ fontSize: 10 }} width={40} allowDecimals={false} />
            <Tooltip content={<ChartTooltip />} />
            <Bar dataKey="count" name="发送" fill="#10b981" radius={[3, 3, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </ChartShell>
  )
}

export function RepliesBouncesChart({ replies, bounces }: { replies: SeriesPoint[]; bounces: SeriesPoint[] }) {
  return (
    <ChartShell title="30 天回复 / 退信" desc="inbound 回复与硬退信走势">
      <div className="h-52">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
            <XAxis dataKey="day" tickFormatter={dayShort} tick={{ fontSize: 10 }} interval="preserveStartEnd" />
            <YAxis tick={{ fontSize: 10 }} width={40} allowDecimals={false} />
            <Tooltip content={<ChartTooltip />} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line data={replies} dataKey="count" name="回复" stroke="#f59e0b" strokeWidth={2} dot={false} />
            <Line data={bounces} dataKey="count" name="退信" stroke="#ef4444" strokeWidth={2} dot={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </ChartShell>
  )
}

// ── Distribution charts ───────────────────────────────────────────────────

export function SourcePieChart({ data }: { data: NameCount[] }) {
  const rows = data.map((d) => ({ name: sourceZh(d.name), count: d.count }))
  return (
    <ChartShell title="来源分布" desc="按数据源统计线索">
      <div className="h-56">
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie data={rows} dataKey="count" nameKey="name" innerRadius={45} outerRadius={75} paddingAngle={2}>
              {rows.map((_, i) => (
                <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
              ))}
            </Pie>
            <Tooltip content={<ChartTooltip noLabel />} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
          </PieChart>
        </ResponsiveContainer>
      </div>
    </ChartShell>
  )
}

export function StatusPieChart({ data }: { data: NameCount[] }) {
  return (
    <ChartShell title="状态分布" desc="线索漏斗状态">
      <div className="h-56">
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie data={data} dataKey="count" nameKey="name" innerRadius={45} outerRadius={75} paddingAngle={2}>
              {data.map((_, i) => (
                <Cell key={i} fill={PALETTE[(i + 3) % PALETTE.length]} />
              ))}
            </Pie>
            <Tooltip content={<ChartTooltip noLabel />} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
          </PieChart>
        </ResponsiveContainer>
      </div>
    </ChartShell>
  )
}

export function CountryBarChart({ data }: { data: NameCount[] }) {
  const rows = data.map((d) => ({ name: d.name === '' ? '未知' : d.name, count: d.count }))
  return (
    <ChartShell title="国家分布 TOP15" desc="线索最多的 15 个国家（其余并入前端合计）">
      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 8, left: 8, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" horizontal={false} />
            <XAxis type="number" tick={{ fontSize: 10 }} allowDecimals={false} />
            <YAxis type="category" dataKey="name" tick={{ fontSize: 10 }} width={90} />
            <Tooltip content={<ChartTooltip />} />
            <Bar dataKey="count" name="线索" fill="#8b5cf6" radius={[0, 3, 3, 0]} barSize={14} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </ChartShell>
  )
}

export function MailboxBarChart({ data }: { data: NameCount[] }) {
  const rows = data.map((d) => ({ name: MAILBOX_ZH[d.name] ?? d.name, count: d.count }))
  return (
    <ChartShell title="邮箱用量分布（30 天）" desc="各发信账号发出的邮件数">
      <div className="h-52">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
            <XAxis dataKey="name" tick={{ fontSize: 10 }} />
            <YAxis tick={{ fontSize: 10 }} width={40} allowDecimals={false} />
            <Tooltip content={<ChartTooltip />} />
            <Bar dataKey="count" name="邮件" fill="#06b6d4" radius={[3, 3, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </ChartShell>
  )
}

// ── Page-level skeleton ───────────────────────────────────────────────────

export function ChartsSkeleton() {
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {Array.from({ length: 6 }).map((_, i) => (
        <Skeleton key={i} className="h-72 rounded-xl" />
      ))}
    </div>
  )
}

/** 把 8 个图排布成两行网格。 */
export function ChartsGrid({ overview }: { overview: DashboardOverview | undefined }) {
  if (!overview) return <ChartsSkeleton />
  const s = overview.series_30d
  const d = overview.distributions
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <LeadsCreatedChart data={s.leads_created} />
      <EmailsSentChart data={s.emails_sent} />
      <RepliesBouncesChart replies={s.replies} bounces={s.bounces} />
      <SourcePieChart data={d.by_source} />
      <StatusPieChart data={d.by_status} />
      <CountryBarChart data={d.by_country} />
      <MailboxBarChart data={d.by_mailbox} />
    </div>
  )
}
