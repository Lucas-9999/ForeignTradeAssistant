import { useState } from 'react'
import { Mail, Phone, Globe, MessageSquare, ChevronDown, ChevronUp, X, Tag as TagIcon, Users } from 'lucide-react'
import { Drawer } from '@/components/ui/drawer'
import { Badge } from '@/components/ui/badge'
import { Select } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import {
  LEAD_STATUS_ZH,
  STATUS_BADGE_VARIANT,
  COMM_DIRECTION_ZH,
  COMM_STATUS_ZH,
  sourceZh,
} from '@/lib/zh'
import { formatDate, formatDateTime } from '@/lib/utils'
import { useLeadCommunications } from '@/hooks/use-leads'
import {
  useTags,
  useGroups,
  useAddTagLeads,
  useRemoveTagLeads,
  useAddGroupLeads,
  useRemoveGroupLeads,
} from '@/hooks/use-tags-groups'
import type { LeadItem } from '@/types'
import type { Communication as Comm } from '@/services/leads'
import type { TagRow, GroupRow } from '@/services/tags-groups'

function InfoRow({ label, value }: { label: string; value?: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-3 py-1.5">
      <span className="shrink-0 text-sm text-muted-foreground">{label}</span>
      <span className="min-w-0 break-all text-right text-sm font-medium">{value ?? '—'}</span>
    </div>
  )
}

function LinkValue({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noreferrer" className="text-primary underline-offset-2 hover:underline">
      {children}
    </a>
  )
}

/** 单条沟通记录 —— 方向色条 + 主题 + 状态 + 可展开正文。 */
function TimelineItem({ c }: { c: Comm }) {
  const [expanded, setExpanded] = useState(false)
  const inbound = c.direction === 'inbound'
  const body = c.body ?? ''
  const truncated = body.length > 400
  return (
    <div className="relative flex gap-3 rounded-lg border p-3">
      <span
        className={`mt-1 h-2.5 w-2.5 shrink-0 rounded-full ${inbound ? 'bg-emerald-500' : 'bg-blue-500'}`}
        title={inbound ? '收到' : '发出'}
      />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium">{c.subject || '(无主题)'}</span>
          <Badge variant={inbound ? 'success' : 'default'}>{COMM_DIRECTION_ZH[c.direction] ?? c.direction}</Badge>
          {c.status ? <Badge variant="outline">{COMM_STATUS_ZH[c.status] ?? c.status}</Badge> : null}
        </div>
        <div className="mt-0.5 text-xs text-muted-foreground">{formatDateTime(c.sent_at)}</div>
        {body ? (
          <div className="mt-2 whitespace-pre-wrap rounded-md bg-muted/40 p-2 text-xs leading-relaxed text-foreground/80">
            {expanded || !truncated ? body : `${body.slice(0, 400)}…`}
          </div>
        ) : null}
        {truncated ? (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="mt-1 flex items-center gap-0.5 text-xs text-primary hover:underline"
          >
            {expanded ? (
              <>
                收起 <ChevronUp className="h-3 w-3" />
              </>
            ) : (
              <>
                展开全文 <ChevronDown className="h-3 w-3" />
              </>
            )}
          </button>
        ) : null}
        {c.status === 'failed' && c.error_message ? (
          <div className="mt-1.5 rounded bg-destructive/10 px-2 py-1 text-xs text-destructive">{c.error_message}</div>
        ) : null}
      </div>
    </div>
  )
}

function CloseBadge({ label, color, onRemove, busy }: { label: string; color: string; onRemove: () => void; busy?: boolean }) {
  return (
    <span
      className="inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-medium"
      style={{ borderColor: `${color}55`, background: `${color}1a`, color }}
    >
      <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: color }} />
      {label}
      <button type="button" onClick={onRemove} disabled={busy} className="ml-0.5 rounded-sm hover:bg-black/10 disabled:opacity-40" aria-label={`移除 ${label}`}>
        <X className="h-3 w-3" />
      </button>
    </span>
  )
}

/** 标签区：展示已打标签 + 可移除 + 下拉添加新标签。 */
function TagSection({ lead }: { lead: LeadItem }) {
  const { data: tagData } = useTags()
  const addTag = useAddTagLeads()
  const removeTag = useRemoveTagLeads()
  if (!tagData?.available) return null
  const current = lead.tags ?? []
  const currentIds = new Set(current.map((t) => t.id))
  const available = tagData.tags.filter((t) => !currentIds.has(t.id))
  const busy = addTag.isPending || removeTag.isPending

  return (
    <section>
      <h3 className="mb-2 flex items-center gap-1 text-sm font-semibold text-muted-foreground">
        <TagIcon className="h-3.5 w-3.5" /> 标签
      </h3>
      <div className="rounded-lg border p-3">
        {current.length === 0 ? (
          <div className="mb-2 text-xs text-muted-foreground">暂无标签</div>
        ) : (
          <div className="mb-2 flex flex-wrap items-center gap-1.5">
            {current.map((t) => (
              <CloseBadge
                key={t.id}
                label={t.name}
                color={t.color || '#64748b'}
                busy={busy}
                onRemove={() => removeTag.mutate({ id: t.id, leadIds: [lead.id] })}
              />
            ))}
          </div>
        )}
        {available.length > 0 ? (
          <Select
            value=""
            placeholder="添加标签…"
            disabled={busy}
            options={available.map((t: TagRow) => ({ value: String(t.id), label: t.name }))}
            onChange={(e) => {
              const id = Number(e.target.value)
              if (id) addTag.mutate({ id, leadIds: [lead.id] })
            }}
          />
        ) : (
          <div className="text-xs text-muted-foreground">已使用全部标签（可在「标签」页新建）</div>
        )}
      </div>
    </section>
  )
}

/** 分组区：展示所属分组 + 可移除 + 下拉加入分组。 */
function GroupSection({ lead }: { lead: LeadItem }) {
  const { data: groupData } = useGroups()
  const addGroup = useAddGroupLeads()
  const removeGroup = useRemoveGroupLeads()
  if (!groupData?.available) return null
  const current = lead.groups ?? []
  const currentIds = new Set(current.map((g) => g.id))
  const available = groupData.groups.filter((g) => !currentIds.has(g.id))
  const busy = addGroup.isPending || removeGroup.isPending

  return (
    <section>
      <h3 className="mb-2 flex items-center gap-1 text-sm font-semibold text-muted-foreground">
        <Users className="h-3.5 w-3.5" /> 所属分组
      </h3>
      <div className="rounded-lg border p-3">
        {current.length === 0 ? (
          <div className="mb-2 text-xs text-muted-foreground">未加入任何分组</div>
        ) : (
          <div className="mb-2 flex flex-wrap items-center gap-1.5">
            {current.map((g) => (
              <CloseBadge
                key={g.id}
                label={g.name}
                color={g.color || '#64748b'}
                busy={busy}
                onRemove={() => removeGroup.mutate({ id: g.id, leadIds: [lead.id] })}
              />
            ))}
          </div>
        )}
        {available.length > 0 ? (
          <Select
            value=""
            placeholder="加入分组…"
            disabled={busy}
            options={available.map((g: GroupRow) => ({ value: String(g.id), label: g.name }))}
            onChange={(e) => {
              const id = Number(e.target.value)
              if (id) addGroup.mutate({ id, leadIds: [lead.id] })
            }}
          />
        ) : (
          <div className="text-xs text-muted-foreground">已加入全部分组（可在「分组」页新建）</div>
        )}
      </div>
    </section>
  )
}

export function LeadDetailDrawer({ lead, open, onClose }: { lead: LeadItem | null; open: boolean; onClose: () => void }) {
  const { data: commData, isLoading } = useLeadCommunications(open && lead ? lead.id : null)
  const comms = commData?.communications ?? []

  return (
    <Drawer open={open} onClose={onClose} title={lead ? lead.company_name ?? `线索 #${lead.id}` : '线索详情'}>
      {!lead ? (
        <Skeleton className="h-64 rounded-xl" />
      ) : (
        <div className="space-y-6">
          {/* 基本信息 */}
          <section>
            <h3 className="mb-2 text-sm font-semibold text-muted-foreground">基本信息</h3>
            <div className="rounded-lg border p-3">
              <div className="mb-2 flex items-center gap-2">
                <Badge variant={STATUS_BADGE_VARIANT[lead.status] ?? 'outline'}>
                  {LEAD_STATUS_ZH[lead.status] ?? lead.status}
                </Badge>
                <span className="text-xs text-muted-foreground">来源 {sourceZh(lead.source)}</span>
              </div>
              <InfoRow label="公司" value={lead.company_name} />
              <InfoRow label="联系人" value={lead.contact_name} />
              <InfoRow
                label="邮箱"
                value={lead.email ? <LinkValue href={`mailto:${lead.email}`}>{lead.email}</LinkValue> : undefined}
              />
              <InfoRow label="电话" value={lead.phone} />
              <InfoRow label="WhatsApp" value={lead.whatsapp} />
              <InfoRow
                label="网站"
                value={lead.website ? <LinkValue href={lead.website}>{lead.website}</LinkValue> : undefined}
              />
              <InfoRow label="国家" value={lead.country} />
              <InfoRow label="行业" value={lead.industry} />
              <InfoRow label="发现时间" value={formatDate(lead.discovered_at)} />
              <InfoRow label="最近活动" value={formatDate(lead.last_activity_at)} />
            </div>
          </section>

          {/* 联系指标 */}
          <section>
            <h3 className="mb-2 text-sm font-semibold text-muted-foreground">联系情况</h3>
            <div className="grid grid-cols-3 gap-2">
              <div className="rounded-lg border p-3 text-center">
                <div className="flex items-center justify-center gap-1 text-lg font-semibold">
                  <Mail className="h-4 w-4 text-primary" />
                  {lead.sent_count}
                </div>
                <div className="text-xs text-muted-foreground">已发邮件</div>
              </div>
              <div className="rounded-lg border p-3 text-center">
                <div className="flex items-center justify-center gap-1 text-lg font-semibold">
                  <MessageSquare className="h-4 w-4 text-emerald-500" />
                  {lead.reply_count}
                </div>
                <div className="text-xs text-muted-foreground">收到回复</div>
              </div>
              <div className="rounded-lg border p-3 text-center">
                <div className="flex items-center justify-center gap-1 text-lg font-semibold">
                  <Phone className="h-4 w-4 text-muted-foreground" />
                  {lead.phone || lead.whatsapp ? '有' : '—'}
                </div>
                <div className="text-xs text-muted-foreground">电话/WhatsApp</div>
              </div>
            </div>
          </section>

          {/* 标签 / 分组（M4 打标） */}
          <TagSection lead={lead} />
          <GroupSection lead={lead} />

          {/* 联系时间线 */}
          <section>
            <h3 className="mb-2 text-sm font-semibold text-muted-foreground">
              联系时间线
              {isLoading ? '' : `（${comms.length} 条）`}
            </h3>
            {isLoading ? (
              <div className="space-y-2">
                <Skeleton className="h-20 rounded-lg" />
                <Skeleton className="h-20 rounded-lg" />
              </div>
            ) : comms.length === 0 ? (
              <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
                暂无沟通记录
              </div>
            ) : (
              <div className="space-y-2">
                {comms.map((c: Comm) => (
                  <TimelineItem key={c.id} c={c} />
                ))}
              </div>
            )}
          </section>

          {/* 备注 */}
          {lead.notes ? (
            <section>
              <h3 className="mb-2 text-sm font-semibold text-muted-foreground">备注</h3>
              <div className="flex items-start gap-2 rounded-lg border p-3 text-sm">
                <Globe className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                <span className="whitespace-pre-wrap text-foreground/80">{lead.notes}</span>
              </div>
            </section>
          ) : null}
        </div>
      )}
    </Drawer>
  )
}
