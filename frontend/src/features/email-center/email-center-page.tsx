import { useMemo, useState } from 'react'
import { AlertTriangle, CheckCircle2, Eye, Loader2, MailPlus, ShieldCheck } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { PagePlaceholder } from '@/features/common/page-placeholder'
import {
  useEmailAudit,
  useEmailCenterOptions,
  useEmailJob,
  usePreviewEmailBatch,
  useStartEmailBatch,
} from '@/hooks/use-email-center'
import { formatDateTime } from '@/lib/utils'
import type { EmailCenterPreviewInput } from '@/types'

const COUNTRY_ZH: Record<string, string> = {
  north_america: '北美', europe: '欧洲', northeast_asia: '东北亚', oceania: '大洋洲',
  southeast_asia: '东南亚', middle_east: '中东', generic: '其他地区',
}

const INDUSTRY_ZH: Record<string, string> = {
  automotive: '汽车', machinery: '机械设备', construction: '建筑', electronics: '电子',
  new_energy: '新能源', medical: '医疗', hardware_furniture: '五金家具', general: '通用',
}

const EVENT_ZH: Record<string, string> = {
  preview_created: '生成预览', send_started: '开始发送', send_completed: '发送完成', send_failed: '发送失败',
}

/** M6 Email Center — preview first, then one-time confirmation + operator token. */
export function EmailCenterPage() {
  const optionsQuery = useEmailCenterOptions()
  const previewMutation = usePreviewEmailBatch()
  const startMutation = useStartEmailBatch()
  const auditQuery = useEmailAudit()

  const [mailbox, setMailbox] = useState('default')
  const [limit, setLimit] = useState('10')
  const [countryFilter, setCountryFilter] = useState('')
  const [countryGroup, setCountryGroup] = useState('')
  const [industryGroup, setIndustryGroup] = useState('')
  const [templateId, setTemplateId] = useState('')
  const [source, setSource] = useState('')
  const [includeLow, setIncludeLow] = useState(false)
  const [confirmation, setConfirmation] = useState('')
  const [authorization, setAuthorization] = useState('')
  const [previewKey, setPreviewKey] = useState<string | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [sampleIndex, setSampleIndex] = useState(0)
  const jobQuery = useEmailJob(jobId)

  const request = useMemo<EmailCenterPreviewInput>(() => ({
    mailbox,
    limit: Math.max(1, Number(limit) || 1),
    country_filter: countryFilter.trim() || undefined,
    country_group: countryGroup || undefined,
    industry_group: industryGroup || undefined,
    include_low_relevance: includeLow,
    template_id: templateId || undefined,
    source: source.trim() || undefined,
  }), [mailbox, limit, countryFilter, countryGroup, industryGroup, includeLow, templateId, source])
  const requestKey = JSON.stringify(request)
  const preview = previewMutation.data
  const previewIsCurrent = Boolean(preview && previewKey === requestKey)
  const selectedSample = preview?.samples[sampleIndex] ?? preview?.samples[0]

  if (optionsQuery.isLoading) {
    return <div className="space-y-4"><Skeleton className="h-20 rounded-xl" /><Skeleton className="h-80 rounded-xl" /></div>
  }
  if (optionsQuery.isError || !optionsQuery.data) {
    return <PagePlaceholder title="邮件中心" milestone="M6" description="邮件中心配置加载失败，请确认后端已启动。" />
  }

  const options = optionsQuery.data
  const mailboxOptions = options.mailboxes.map((m) => ({
    value: m.name,
    label: `${m.display_name || m.name}（剩余 ${m.remaining}/${m.daily_limit}）`,
  }))
  const selectedMailbox = options.mailboxes.find((m) => m.name === mailbox)
  const canConfirm = Boolean(
    options.send_enabled &&
    previewIsCurrent &&
    preview?.confirmation_id &&
    preview.confirmation_text === confirmation &&
    authorization,
  )

  const runPreview = async () => {
    const result = await previewMutation.mutateAsync(request)
    setPreviewKey(requestKey)
    setConfirmation('')
    setSampleIndex(0)
    return result
  }

  const runSend = async () => {
    if (!preview?.confirmation_id || !preview.confirmation_text || !canConfirm) return
    const result = await startMutation.mutateAsync({
      confirmationId: preview.confirmation_id,
      confirmationText: confirmation,
      authorizationCode: authorization,
    })
    setJobId(result.job_id)
    setAuthorization('')
    setConfirmation('')
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">邮件中心</h1>
          <p className="mt-1 text-sm text-muted-foreground">先预览并冻结收件人，再授权确认；发送复用现有批量发信、配额与反垃圾链路。</p>
        </div>
        <Badge variant={options.send_enabled ? 'success' : 'secondary'}>
          <ShieldCheck className="mr-1 h-3.5 w-3.5" />
          {options.send_enabled ? 'Web 发信已启用' : 'Web 发信已锁定'}
        </Badge>
      </div>

      {!options.send_enabled ? (
        <div className="flex gap-3 rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <div><strong>安全锁已生效。</strong> 配置环境变量 <code>FTA_UI_SEND_TOKEN</code> 并重启后端后，才可从 Web 确认真实发送；预览仍可使用。</div>
        </div>
      ) : null}

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(360px,0.72fr)]">
        <Card>
          <CardHeader>
            <CardTitle>1. 选择发送范围</CardTitle>
            <CardDescription>最多 {options.max_batch_size} 封；实际发送还会再次检查 MX、黑名单、重复联系和邮箱额度。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <Field label="发件邮箱">
                <Select value={mailbox} onChange={(e) => setMailbox(e.target.value)} options={mailboxOptions} />
              </Field>
              <Field label="本次上限">
                <Input type="number" min={1} max={options.max_batch_size} value={limit} onChange={(e) => setLimit(e.target.value)} />
              </Field>
              <Field label="邮件模板">
                <Select
                  value={templateId}
                  onChange={(e) => setTemplateId(e.target.value)}
                  placeholder="请选择具体模板"
                  options={options.templates.map((template) => ({
                    value: template.id,
                    label: `${template.name}（${template.category} / ${template.language}）`,
                  }))}
                />
              </Field>
              <Field label="国家分组">
                <Select value={countryGroup} onChange={(e) => setCountryGroup(e.target.value)} placeholder="全部" options={options.country_groups.map((x) => ({ value: x, label: COUNTRY_ZH[x] ?? x }))} />
              </Field>
              <Field label="行业分组">
                <Select value={industryGroup} onChange={(e) => setIndustryGroup(e.target.value)} placeholder="全部" options={options.industry_groups.map((x) => ({ value: x, label: INDUSTRY_ZH[x] ?? x }))} />
              </Field>
              <Field label="精确国家">
                <Input value={countryFilter} onChange={(e) => setCountryFilter(e.target.value)} placeholder="例如 Germany" />
              </Field>
              <Field label="线索来源">
                <Input value={source} onChange={(e) => setSource(e.target.value)} placeholder="留空使用邮箱默认规则" />
              </Field>
              <label className="flex items-end gap-2 pb-2 text-sm">
                <input type="checkbox" checked={includeLow} onChange={(e) => setIncludeLow(e.target.checked)} />
                包含低相关行业
              </label>
            </div>
            {selectedMailbox ? (
              <div className="rounded-md bg-muted/60 px-3 py-2 text-xs text-muted-foreground">
                当前邮箱剩余额度：{selectedMailbox.remaining}；凭证来源：{selectedMailbox.credential_source}。
              </div>
            ) : null}
            {previewMutation.error ? <ErrorLine error={previewMutation.error} /> : null}
            <Button onClick={() => void runPreview()} disabled={previewMutation.isPending || !templateId}>
              {previewMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Eye className="h-4 w-4" />}
              生成预览
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>2. 预览与冻结</CardTitle>
            <CardDescription>修改左侧条件后必须重新生成预览，旧确认不可继续使用。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {!preview ? <p className="py-16 text-center text-sm text-muted-foreground">尚未生成预览</p> : (
              <>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-muted-foreground">候选收件人</span>
                  <span className="text-2xl font-semibold tabular-nums">{preview.candidate_count}</span>
                </div>
                <p className="text-xs text-muted-foreground">{preview.message}</p>
                {!previewIsCurrent ? <div className="rounded-md bg-amber-50 p-2 text-xs text-amber-900">筛选条件已变化，请重新预览。</div> : null}
                {preview.samples.length ? (
                  <>
                    <Select
                      value={String(sampleIndex)}
                      onChange={(e) => setSampleIndex(Number(e.target.value))}
                      options={preview.samples.map((x, i) => ({ value: String(i), label: `${x.company_name || '未命名公司'} · ${x.email}` }))}
                    />
                    {selectedSample ? (
                      <div className="space-y-2 rounded-lg border p-3">
                        <div className="text-xs text-muted-foreground">主题</div>
                        <div className="text-sm font-medium">{selectedSample.subject}</div>
                        <iframe title="邮件正文预览" sandbox="" srcDoc={selectedSample.body_html} className="h-56 w-full rounded border bg-white" />
                      </div>
                    ) : null}
                  </>
                ) : null}
              </>
            )}
          </CardContent>
        </Card>
      </div>

      {preview?.confirmation_id && previewIsCurrent ? (
        <Card className="border-primary/30">
          <CardHeader>
            <CardTitle>3. 授权确认</CardTitle>
            <CardDescription>确认记录十分钟内有效且只能使用一次。授权码仅保存在当前页面内存中。</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4 md:grid-cols-3">
            <Field label={`输入：${preview.confirmation_text}`}>
              <Input value={confirmation} onChange={(e) => setConfirmation(e.target.value)} autoComplete="off" />
            </Field>
            <Field label="发信授权码">
              <Input type="password" value={authorization} onChange={(e) => setAuthorization(e.target.value)} autoComplete="off" />
            </Field>
            <div className="flex items-end">
              <Button variant="destructive" className="w-full" disabled={!canConfirm || startMutation.isPending} onClick={() => void runSend()}>
                {startMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <MailPlus className="h-4 w-4" />}
                确认并开始发送
              </Button>
            </div>
            {startMutation.error ? <div className="md:col-span-3"><ErrorLine error={startMutation.error} /></div> : null}
          </CardContent>
        </Card>
      ) : null}

      {jobId ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              {jobQuery.data?.status === 'completed' ? <CheckCircle2 className="h-4 w-4 text-emerald-600" /> : <Loader2 className="h-4 w-4 animate-spin" />}
              发送任务
            </CardTitle>
            <CardDescription>任务编号 {jobId}</CardDescription>
          </CardHeader>
          <CardContent>
            {jobQuery.data ? (
              <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-5">
                <Metric label="状态" value={jobQuery.data.status} />
                <Metric label="请求" value={jobQuery.data.requested} />
                <Metric label="已发送" value={jobQuery.data.sent} />
                <Metric label="跳过" value={jobQuery.data.skipped} />
                <Metric label="错误" value={jobQuery.data.errors} />
                {jobQuery.data.error ? <div className="col-span-full text-sm text-destructive">{jobQuery.data.error}</div> : null}
              </div>
            ) : <Skeleton className="h-16" />}
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader><CardTitle>最近审计记录</CardTitle><CardDescription>只记录任务元数据，不记录密码、授权码、邮件正文或收件人。</CardDescription></CardHeader>
        <CardContent>
          {auditQuery.data?.items.length ? (
            <div className="divide-y rounded-lg border">
              {auditQuery.data.items.map((item, i) => (
                <div key={`${item.at}-${i}`} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-3 py-2 text-sm">
                  <Badge variant="outline">{EVENT_ZH[item.event] ?? item.event}</Badge>
                  <span>{item.mailbox ?? '—'}</span>
                  <span className="text-muted-foreground">{formatDateTime(item.at)}</span>
                  {item.sent !== undefined ? <span className="ml-auto">发送 {item.sent} · 跳过 {item.skipped ?? 0} · 错误 {item.errors ?? 0}</span> : null}
                </div>
              ))}
            </div>
          ) : <p className="py-6 text-center text-sm text-muted-foreground">暂无 Email Center 审计记录</p>}
        </CardContent>
      </Card>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="flex flex-col gap-1 text-xs text-muted-foreground"><span>{label}</span>{children}</label>
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return <div className="rounded-md bg-muted/60 p-3"><div className="text-xs text-muted-foreground">{label}</div><div className="mt-1 font-semibold tabular-nums">{value}</div></div>
}

function ErrorLine({ error }: { error: Error }) {
  return <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">{error.message}</div>
}
