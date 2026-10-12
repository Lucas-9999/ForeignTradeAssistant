import { useEffect, useState, useMemo } from 'react'
// v9 ships the v8-style API as the `legacy` entry — useLegacyTable + getCoreRowModel.
import { useLegacyTable, getCoreRowModel, type LegacyColumnDef } from '@tanstack/react-table/legacy'
import { flexRender } from '@tanstack/react-table/flex-render'
import { Search, RotateCcw, ChevronLeft, ChevronRight, Send, Reply, Plus, Check } from 'lucide-react'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Card, CardContent } from '@/components/ui/card'
import { useCreateLead, useLeads } from '@/hooks/use-leads'
import { useTags } from '@/hooks/use-tags-groups'
import { LeadDetailDrawer } from './lead-detail-drawer'
import { LEAD_STATUS_ZH, STATUS_BADGE_VARIANT, SOURCE_ZH, sourceZh } from '@/lib/zh'
import { formatDate, formatCount } from '@/lib/utils'
import type { LeadItem } from '@/types'

const PAGE_SIZE = 50

const SOURCE_OPTIONS = Object.entries(SOURCE_ZH).map(([value, label]) => ({ value, label }))
const STATUS_OPTIONS = Object.entries(LEAD_STATUS_ZH).map(([value, label]) => ({ value, label }))
const YES_NO_OPTIONS = [
  { value: '1', label: '是' },
  { value: '0', label: '否' },
]

export function LeadsPage() {
  // ── 筛选状态 ─────────────────────────────────────────────────────────
  const [search, setSearch] = useState('')
  const [country, setCountry] = useState('')
  const [debounced, setDebounced] = useState({ search: '', country: '' })
  const [status, setStatus] = useState('all')
  const [source, setSource] = useState('all')
  const [tag, setTag] = useState('all')
  const [hasSent, setHasSent] = useState('all')
  const [hasReplied, setHasReplied] = useState('all')
  const [page, setPage] = useState(1)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [creating, setCreating] = useState(false)
  const [companyName, setCompanyName] = useState('')
  const [email, setEmail] = useState('')
  const [contactName, setContactName] = useState('')
  const [leadCountry, setLeadCountry] = useState('')
  const [leadIndustry, setLeadIndustry] = useState('')
  const [notes, setNotes] = useState('')
  const [createdLeadId, setCreatedLeadId] = useState<number | null>(null)

  const { data: tagData } = useTags()
  const TAG_OPTIONS = (tagData?.tags ?? []).map((t) => ({ value: t.name, label: t.name }))

  // 搜索/国家输入防抖 400ms，触发后回到第一页。
  useEffect(() => {
    const t = setTimeout(() => {
      setDebounced({ search, country })
      setPage(1)
    }, 400)
    return () => clearTimeout(t)
  }, [search, country])

  const { data, isLoading, isError, error, refetch } = useLeads({
    page,
    page_size: PAGE_SIZE,
    search: debounced.search || undefined,
    country: debounced.country || undefined,
    status: status === 'all' ? undefined : status,
    source: source === 'all' ? undefined : source,
    tag: tag === 'all' ? undefined : tag,
    has_sent: hasSent === 'all' ? undefined : hasSent === '1',
    has_replied: hasReplied === 'all' ? undefined : hasReplied === '1',
  })
  const createLead = useCreateLead()

  const selectedLead = useMemo(() => data?.items.find((l) => l.id === selectedId) ?? null, [data, selectedId])

  // ── TanStack Table（v9 legacy API，服务端分页） ──────────────────────
  const columns = useMemo<LegacyColumnDef<LeadItem, unknown>[]>(
    () => [
      {
        accessorKey: 'company_name',
        header: '公司',
        cell: ({ row }) => (
          <div className="min-w-0">
            <div className="truncate font-medium">{row.original.company_name ?? '—'}</div>
            {row.original.contact_name ? (
              <div className="truncate text-xs text-muted-foreground">{row.original.contact_name}</div>
            ) : null}
          </div>
        ),
      },
      {
        accessorKey: 'email',
        header: '邮箱',
        cell: ({ row }) => {
          const v = row.original.email
          return v ? (
            <a href={`mailto:${v}`} className="truncate text-primary underline-offset-2 hover:underline">
              {v}
            </a>
          ) : (
            <span className="text-muted-foreground">—</span>
          )
        },
      },
      { accessorKey: 'country', header: '国家', cell: ({ row }) => row.original.country || '—' },
      {
        accessorKey: 'source',
        header: '来源',
        cell: ({ row }) => {
          const v = row.original.source
          return v ? <span className="whitespace-nowrap">{sourceZh(v)}</span> : '—'
        },
      },
      {
        accessorKey: 'status',
        header: '状态',
        cell: ({ row }) => {
          const v = row.original.status
          return <Badge variant={STATUS_BADGE_VARIANT[v] ?? 'outline'}>{LEAD_STATUS_ZH[v] ?? v}</Badge>
        },
      },
      {
        id: 'markers',
        header: '已发/已回复',
        cell: ({ row }) => (
          <div className="flex items-center gap-1.5">
            {row.original.has_sent ? (
              <span className="flex items-center gap-1 text-xs text-primary" title={`已发 ${row.original.sent_count} 封`}>
                <Send className="h-3 w-3" />
              </span>
            ) : (
              <span className="text-xs text-muted-foreground/40">—</span>
            )}
            {row.original.has_replied ? (
              <span className="flex items-center gap-1 text-xs text-emerald-600" title={`回复 ${row.original.reply_count} 条`}>
                <Reply className="h-3 w-3" />
              </span>
            ) : null}
          </div>
        ),
      },
      {
        id: 'tags',
        header: '标签',
        cell: ({ row }) => {
          const tags = row.original.tags ?? []
          if (tags.length === 0) return <span className="text-xs text-muted-foreground/40">—</span>
          const shown = tags.slice(0, 3)
          const extra = tags.length - shown.length
          return (
            <div className="flex items-center gap-1">
              {shown.map((t) => (
                <span
                  key={t.id}
                  className="inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px]"
                  style={{ borderColor: `${t.color}55`, background: `${t.color}1a`, color: t.color }}
                >
                  <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: t.color }} />
                  {t.name}
                </span>
              ))}
              {extra > 0 ? <span className="text-xs text-muted-foreground">+{extra}</span> : null}
            </div>
          )
        },
      },
      {
        accessorKey: 'last_activity_at',
        header: '最近活动',
        cell: ({ row }) => (
          <span className="whitespace-nowrap text-xs text-muted-foreground">{formatDate(row.original.last_activity_at)}</span>
        ),
      },
    ],
    [],
  )

  const table = useLegacyTable({
    data: data?.items ?? [],
    columns,
    state: {
      pagination: { pageIndex: page - 1, pageSize: PAGE_SIZE },
    },
    manualPagination: true,
    pageCount: data?.total_pages ?? 0,
    getCoreRowModel: getCoreRowModel(),
  })

  const resetFilters = () => {
    setSearch('')
    setCountry('')
    setStatus('all')
    setSource('all')
    setTag('all')
    setHasSent('all')
    setHasReplied('all')
    setPage(1)
  }

  const submitCreate = () => {
    const company_name = companyName.trim()
    const leadEmail = email.trim()
    if (!company_name || !leadEmail) return
    createLead.mutate(
      {
        company_name,
        email: leadEmail,
        contact_name: contactName.trim() || undefined,
        country: leadCountry.trim() || undefined,
        industry: leadIndustry.trim() || undefined,
        notes: notes.trim() || undefined,
        source: 'manual',
      },
      {
        onSuccess: (lead) => {
          setCreatedLeadId(lead.id)
          setCompanyName('')
          setEmail('')
          setContactName('')
          setLeadCountry('')
          setLeadIndustry('')
          setNotes('')
          setCreating(false)
          setStatus('new')
          setSource('all')
          setPage(1)
        },
      },
    )
  }

  const hasActiveFilters =
    search !== '' || country !== '' || status !== 'all' || source !== 'all' || tag !== 'all' || hasSent !== 'all' || hasReplied !== 'all'

  const totalPages = data?.total_pages ?? 0
  const from = data && data.total > 0 ? (page - 1) * PAGE_SIZE + 1 : 0
  const to = data ? Math.min(page * PAGE_SIZE, data.total) : 0

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">客户线索</h1>
          <p className="text-sm text-muted-foreground">
            {data ? `共 ${formatCount(data.total)} 条线索` : '加载中…'}
          </p>
        </div>
        <Button size="sm" onClick={() => { setCreating((value) => !value); setCreatedLeadId(null) }}>
          <Plus className="h-4 w-4" /> 新增线索
        </Button>
      </div>

      {createdLeadId != null ? (
        <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/5 px-4 py-3 text-sm text-emerald-700">
          已创建手工线索 #{createdLeadId}，状态为“新线索”；不会触发任何邮件或后台任务。
        </div>
      ) : null}

      {createLead.error ? (
        <div className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          {createLead.error instanceof Error ? createLead.error.message : '创建线索失败'}
        </div>
      ) : null}

      {creating ? (
        <Card>
          <CardContent className="space-y-3 pt-4">
            <p className="text-sm text-muted-foreground">仅创建 CRM 线索；不会发送邮件或启动采集。</p>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="flex flex-col gap-1">
                <label className="text-xs text-muted-foreground">公司名称 *</label>
                <Input value={companyName} onChange={(event) => setCompanyName(event.target.value)} placeholder="如：JSX Metal — P12 Test" autoFocus />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-xs text-muted-foreground">邮箱 *</label>
                <Input type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="name@example.com" />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-xs text-muted-foreground">联系人</label>
                <Input value={contactName} onChange={(event) => setContactName(event.target.value)} placeholder="可选" />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-xs text-muted-foreground">国家</label>
                <Input value={leadCountry} onChange={(event) => setLeadCountry(event.target.value)} placeholder="可选，如 China" />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-xs text-muted-foreground">行业</label>
                <Input value={leadIndustry} onChange={(event) => setLeadIndustry(event.target.value)} placeholder="可选" />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-xs text-muted-foreground">备注</label>
                <Input value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="可选" />
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Button size="sm" onClick={submitCreate} disabled={!companyName.trim() || !email.trim() || createLead.isPending}>
                <Check className="h-4 w-4" /> 创建线索
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setCreating(false)} disabled={createLead.isPending}>取消</Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {/* 筛选栏 */}
      <div className="flex flex-wrap items-end gap-3 rounded-lg border bg-card p-3">
        <div className="flex min-w-[200px] flex-1 flex-col gap-1">
          <label className="text-xs text-muted-foreground">搜索</label>
          <div className="relative">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="公司 / 联系人 / 邮箱"
              className="pl-8"
            />
          </div>
        </div>
        <div className="flex w-40 flex-col gap-1">
          <label className="text-xs text-muted-foreground">国家</label>
          <Input value={country} onChange={(e) => setCountry(e.target.value)} placeholder="如 DE / USA" />
        </div>
        <div className="flex w-40 flex-col gap-1">
          <label className="text-xs text-muted-foreground">来源</label>
          {/* 占位项 value='' 会被原生 <select> 选中 → 归一化回 'all' 哨兵，避免序列化成空串/false */}
          <Select value={source} onChange={(e) => { setSource(e.target.value === '' ? 'all' : e.target.value); setPage(1) }} placeholder="全部" options={SOURCE_OPTIONS} />
        </div>
        <div className="flex w-36 flex-col gap-1">
          <label className="text-xs text-muted-foreground">状态</label>
          <Select value={status} onChange={(e) => { setStatus(e.target.value === '' ? 'all' : e.target.value); setPage(1) }} placeholder="全部" options={STATUS_OPTIONS} />
        </div>
        <div className="flex w-36 flex-col gap-1">
          <label className="text-xs text-muted-foreground">标签</label>
          <Select value={tag} onChange={(e) => { setTag(e.target.value === '' ? 'all' : e.target.value); setPage(1) }} placeholder="全部" options={TAG_OPTIONS} disabled={TAG_OPTIONS.length === 0} />
        </div>
        <div className="flex w-32 flex-col gap-1">
          <label className="text-xs text-muted-foreground">已发邮件</label>
          <Select value={hasSent} onChange={(e) => { setHasSent(e.target.value === '' ? 'all' : e.target.value); setPage(1) }} placeholder="全部" options={YES_NO_OPTIONS} />
        </div>
        <div className="flex w-32 flex-col gap-1">
          <label className="text-xs text-muted-foreground">已回复</label>
          <Select value={hasReplied} onChange={(e) => { setHasReplied(e.target.value === '' ? 'all' : e.target.value); setPage(1) }} placeholder="全部" options={YES_NO_OPTIONS} />
        </div>
        {hasActiveFilters ? (
          <Button variant="ghost" size="sm" onClick={resetFilters} className="flex items-center gap-1">
            <RotateCcw className="h-3.5 w-3.5" /> 重置
          </Button>
        ) : null}
      </div>

      {/* 错误态 */}
      {isError ? (
        <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-6 text-center">
          <p className="font-medium text-destructive">线索加载失败</p>
          <p className="mt-1 text-sm text-muted-foreground">{error instanceof Error ? error.message : '未知错误'}</p>
          <Button variant="outline" size="sm" className="mt-3" onClick={() => refetch()}>
            重试
          </Button>
        </div>
      ) : (
        <>
          {/* 表格 */}
          <div className="rounded-lg border bg-card">
            {isLoading ? (
              <div className="space-y-2 p-4">
                {Array.from({ length: 8 }).map((_, i) => (
                  <Skeleton key={i} className="h-11 rounded-md" />
                ))}
              </div>
            ) : data?.items.length === 0 ? (
              <div className="p-10 text-center text-sm text-muted-foreground">
                没有符合条件的线索，请调整筛选条件。
              </div>
            ) : (
              <Table>
                <TableHeader>
                  {table.getHeaderGroups().map((hg) => (
                    <TableRow key={hg.id}>
                      {hg.headers.map((header) => (
                        <TableHead key={header.id}>
                          {flexRender(header.column.columnDef.header, header.getContext())}
                        </TableHead>
                      ))}
                    </TableRow>
                  ))}
                </TableHeader>
                <TableBody>
                  {table.getRowModel().rows.map((row) => (
                    <TableRow
                      key={row.id}
                      onClick={() => setSelectedId(row.original.id)}
                      className="cursor-pointer"
                      data-state={selectedId === row.original.id ? 'selected' : undefined}
                    >
                      {row.getVisibleCells().map((cell) => (
                        <TableCell key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </div>

          {/* 分页 */}
          <div className="flex items-center justify-between text-sm text-muted-foreground">
            <span className="tabular-nums">
              {from}–{to} / {data ? formatCount(data.total) : '—'}
            </span>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>
                <ChevronLeft className="h-4 w-4" /> 上一页
              </Button>
              <span className="tabular-nums">
                {page} / {totalPages || 1}
              </span>
              <Button
                variant="outline"
                size="sm"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              >
                下一页 <ChevronRight className="h-4 w-4" />
              </Button>
            </div>
          </div>
        </>
      )}

      {/* 详情抽屉 */}
      <LeadDetailDrawer lead={selectedLead} open={selectedId != null} onClose={() => setSelectedId(null)} />
    </div>
  )
}
