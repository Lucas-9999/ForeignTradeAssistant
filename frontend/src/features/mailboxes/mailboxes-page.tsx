import { useState } from 'react'
import { Plus, Pencil, Trash2, Check, X, Mail, KeyRound, PlugZap, Loader2 } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import {
  useMailboxes,
  useCreateMailbox,
  useUpdateMailbox,
  useDeleteMailbox,
  useTestMailbox,
} from '@/hooks/use-mailboxes'
import { PagePlaceholder } from '@/features/common/page-placeholder'
import { MAILBOX_ZH } from '@/lib/zh'
import type { Mailbox } from '@/types'

const RESERVED = new Set(['default', 'zoho'])

/** OPT-005：按 credential_source 区分「env 回退」与「未配置」——env 文案只适用于内置邮箱。 */
function credentialText(m: Mailbox): string {
  switch (m.credential_source) {
    case 'encrypted':
      return '已加密保存'
    case 'env':
      return '由环境变量配置'
    default:
      return RESERVED.has(m.name) ? '未配置环境变量' : '未配置密码'
  }
}

interface TestState {
  ok: boolean
  message: string
}

/** 邮箱管理 —— M5：新增任意 SMTP 账号 / 编辑 / 删除 / 测试连接。 */
export function MailboxesPage() {
  const { data, isLoading, isError } = useMailboxes()
  const createMailbox = useCreateMailbox()
  const updateMailbox = useUpdateMailbox()
  const deleteMailbox = useDeleteMailbox()
  const testMailbox = useTestMailbox()

  const [creating, setCreating] = useState(false)

  // 新建表单状态
  const [f, setF] = useState<Record<string, string>>({})
  const setField = (k: string) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setF((prev) => ({ ...prev, [k]: e.target.value }))

  // 编辑态（单卡片）
  const [editingName, setEditingName] = useState<string | null>(null)

  // 测试连接结果（按邮箱名）
  const [tests, setTests] = useState<Record<string, TestState>>({})

  const mutationError = createMailbox.error ?? updateMailbox.error ?? deleteMailbox.error

  const submitCreate = () => {
    const name = (f.name ?? '').trim()
    const smtp_host = (f.smtp_host ?? '').trim()
    const username = (f.username ?? '').trim()
    if (!name || !smtp_host || !username) return
    const useTls = (f.security ?? 'tls') === 'tls'
    createMailbox.mutate(
      {
        name,
        display_name: f.display_name?.trim() || undefined,
        smtp_host,
        smtp_port: Number(f.smtp_port) || (useTls ? 587 : 465),
        use_tls: useTls,
        username,
        password: f.password ? f.password : undefined,
        from_email: f.from_email?.trim() || undefined,
        from_name: f.from_name?.trim() || undefined,
        daily_limit: f.daily_limit ? Number(f.daily_limit) : undefined,
      },
      {
        onSuccess: () => {
          setCreating(false)
          setF({})
        },
      },
    )
  }

  const submitEdit = (m: Mailbox) => {
    const useTls = (f[`e_${m.name}_security`] ?? (m.use_tls ? 'tls' : 'ssl')) === 'tls'
    updateMailbox.mutate(
      {
        name: m.name,
        data: {
          display_name: f[`e_${m.name}_display`] ?? undefined,
          smtp_host: f[`e_${m.name}_host`] ?? undefined,
          smtp_port: f[`e_${m.name}_port`] ? Number(f[`e_${m.name}_port`]) : undefined,
          use_tls: useTls !== m.use_tls ? useTls : undefined,
          username: f[`e_${m.name}_user`] ?? undefined,
          password: f[`e_${m.name}_pw`] !== undefined ? f[`e_${m.name}_pw`] : undefined,
          from_email: f[`e_${m.name}_from_email`] ?? undefined,
          from_name: f[`e_${m.name}_from_name`] ?? undefined,
          daily_limit: f[`e_${m.name}_limit`] ? Number(f[`e_${m.name}_limit`]) : undefined,
        },
      },
      { onSuccess: () => setEditingName(null) },
    )
  }

  const runTest = (name: string) => {
    setTests((prev) => ({ ...prev, [name]: { ok: true, message: '' } }))
    testMailbox.mutate(name, {
      onSuccess: (r) => setTests((prev) => ({ ...prev, [name]: r })),
      onError: (e) =>
        setTests((prev) => ({ ...prev, [name]: { ok: false, message: e instanceof Error ? e.message : '测试失败' } })),
    })
  }

  if (isLoading) {
    return (
      <div className="grid gap-4 sm:grid-cols-2">
        {Array.from({ length: 2 }).map((_, i) => (
          <Skeleton key={i} className="h-56 rounded-xl" />
        ))}
      </div>
    )
  }

  if (isError || !data) {
    return (
      <PagePlaceholder
        title="邮箱管理"
        milestone="M5"
        description="邮箱数据加载失败，请确认后端 :8000 已启动。"
      />
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">邮箱管理</h1>
          <p className="text-sm text-muted-foreground">
            共 {data.mailboxes.length} 个发信账号 · 支持任意 SMTP 账号 · 密码只存不读（Fernet 加密）
          </p>
        </div>
        <Button size="sm" onClick={() => setCreating((v) => !v)}>
          <Plus className="h-4 w-4" /> 新增邮箱
        </Button>
      </div>

      {mutationError ? (
        <div className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          {mutationError instanceof Error ? mutationError.message : '操作失败'}
        </div>
      ) : null}

      {/* 新建表单 */}
      {creating ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">新增发信邮箱</CardTitle>
            <CardDescription>
              密码经 FTA_MAILBOX_ENC_KEY 加密后存储；未设置该密钥时后端会返回提示（发信不受影响）。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="邮箱名 *" hint="唯一标识，如 sales" value={f.name ?? ''} onChange={setField('name')} placeholder="sales" />
              <Field label="显示名称" value={f.display_name ?? ''} onChange={setField('display_name')} placeholder="销售组" />
              <Field label="SMTP 服务器 *" value={f.smtp_host ?? ''} onChange={setField('smtp_host')} placeholder="smtp.example.com" />
              <div className="flex gap-3">
                <div className="flex flex-col gap-1">
                  <label className="text-xs text-muted-foreground">加密方式</label>
                  <Select
                    options={[
                      { value: 'tls', label: 'STARTTLS（587）' },
                      { value: 'ssl', label: 'SSL（465）' },
                    ]}
                    value={f.security ?? 'tls'}
                    onChange={(e) => setF((prev) => ({ ...prev, security: e.target.value }))}
                  />
                </div>
                <div className="flex flex-1 flex-col gap-1">
                  <label className="text-xs text-muted-foreground">端口</label>
                  <Input value={f.smtp_port ?? ''} onChange={setField('smtp_port')} placeholder="587" inputMode="numeric" />
                </div>
              </div>
              <Field label="登录账号 *" value={f.username ?? ''} onChange={setField('username')} placeholder="you@example.com" />
              <Field label="密码（可选）" type="password" value={f.password ?? ''} onChange={setField('password')} placeholder="留空 = 走 env 凭证" />
              <Field label="发件邮箱" value={f.from_email ?? ''} onChange={setField('from_email')} placeholder="默认同登录账号" />
              <Field label="发件人名称" value={f.from_name ?? ''} onChange={setField('from_name')} placeholder="如 Sales Team" />
              <Field label="每日限额（可选）" value={f.daily_limit ?? ''} onChange={setField('daily_limit')} placeholder="默认 200" inputMode="numeric" />
            </div>
            <div className="flex items-center gap-2">
              <Button size="sm" onClick={submitCreate} disabled={!f.name?.trim() || !f.smtp_host?.trim() || !f.username?.trim() || createMailbox.isPending}>
                <Check className="h-4 w-4" /> 创建
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setCreating(false)}>
                取消
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {/* 邮箱卡片 */}
      <div className="grid gap-4 sm:grid-cols-2">
        {data.mailboxes.map((m) => (
          <Card key={m.name}>
            <CardHeader>
              <CardTitle className="flex items-center justify-between text-base">
                <span className="flex items-center gap-2">
                  <Mail className="h-4 w-4 text-primary" />
                  {m.display_name || MAILBOX_ZH[m.name] || m.name}
                  {RESERVED.has(m.name) ? <Badge variant="outline">内置</Badge> : null}
                </span>
                <Badge variant={m.is_active ? 'default' : 'secondary'}>{m.is_active ? '启用' : '停用'}</Badge>
              </CardTitle>
              <CardDescription>{m.username}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              {editingName === m.name ? (
                <div className="space-y-3">
                  <div className="grid gap-3 sm:grid-cols-2">
                    <Field label="显示名称" value={f[`e_${m.name}_display`] ?? m.display_name ?? ''} onChange={setField(`e_${m.name}_display`)} />
                    <Field label="SMTP 服务器" value={f[`e_${m.name}_host`] ?? m.smtp_host} onChange={setField(`e_${m.name}_host`)} />
                    <Field label="登录账号" value={f[`e_${m.name}_user`] ?? m.username} onChange={setField(`e_${m.name}_user`)} />
                    <Field
                      label="新密码（留空不变）"
                      type="password"
                      value={f[`e_${m.name}_pw`] ?? ''}
                      onChange={setField(`e_${m.name}_pw`)}
                      placeholder={m.has_smtp_password ? '已配置，留空不改' : '未配置，可填密码'}
                    />
                    <div className="flex gap-3">
                      <div className="flex flex-col gap-1">
                        <label className="text-xs text-muted-foreground">加密</label>
                        <Select
                          options={[
                            { value: 'tls', label: 'STARTTLS（587）' },
                            { value: 'ssl', label: 'SSL（465）' },
                          ]}
                          value={f[`e_${m.name}_security`] ?? (m.use_tls ? 'tls' : 'ssl')}
                          onChange={(e) => setF((prev) => ({ ...prev, [`e_${m.name}_security`]: e.target.value }))}
                        />
                      </div>
                      <div className="flex flex-1 flex-col gap-1">
                        <label className="text-xs text-muted-foreground">端口</label>
                        <Input value={f[`e_${m.name}_port`] ?? String(m.smtp_port)} onChange={setField(`e_${m.name}_port`)} inputMode="numeric" />
                      </div>
                    </div>
                    <Field label="发件邮箱" value={f[`e_${m.name}_from_email`] ?? m.from_email} onChange={setField(`e_${m.name}_from_email`)} />
                    <Field label="发件人名称" value={f[`e_${m.name}_from_name`] ?? m.from_name} onChange={setField(`e_${m.name}_from_name`)} />
                    <Field label="每日限额" value={f[`e_${m.name}_limit`] ?? String(m.daily_limit)} onChange={setField(`e_${m.name}_limit`)} inputMode="numeric" />
                  </div>
                  <div className="flex items-center gap-2">
                    <Button size="sm" onClick={() => submitEdit(m)} disabled={updateMailbox.isPending}>
                      <Check className="h-4 w-4" /> 保存
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setEditingName(null)}>
                      <X className="h-4 w-4" /> 取消
                    </Button>
                  </div>
                </div>
              ) : (
                <>
                  <div className="flex items-center justify-between">
                    <span className="text-muted-foreground">SMTP</span>
                    <span className="font-medium">
                      {m.smtp_host}:{m.smtp_port}
                      {m.use_tls ? ' (TLS)' : ' (SSL)'}
                    </span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="flex items-center gap-1 text-muted-foreground">
                      <KeyRound className="h-3.5 w-3.5" /> 密码
                    </span>
                    <span className="font-medium">{credentialText(m)}</span>
                  </div>
                  <div>
                    <div className="flex items-center justify-between">
                      <span className="text-muted-foreground">今日用量</span>
                      <span className="font-medium tabular-nums">
                        {m.sent_today} / {m.daily_limit}
                      </span>
                    </div>
                    <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full rounded-full bg-primary"
                        style={{ width: `${m.daily_limit > 0 ? Math.min(100, Math.round((m.sent_today / m.daily_limit) * 100)) : 0}%` }}
                      />
                    </div>
                  </div>
                  <div className="flex items-center justify-between text-xs text-muted-foreground">
                    <span>剩余额度</span>
                    <span className="tabular-nums">{m.remaining}</span>
                  </div>
                </>
              )}

              {tests[m.name] ? (
                <div
                  className={`rounded-md border px-3 py-2 text-xs ${
                    tests[m.name].ok
                      ? 'border-emerald-500/40 bg-emerald-500/5 text-emerald-600'
                      : 'border-destructive/40 bg-destructive/5 text-destructive'
                  }`}
                >
                  {tests[m.name].message || '正在连接…'}
                </div>
              ) : null}

              {editingName !== m.name ? (
                <div className="flex items-center gap-2 pt-1">
                  <Button size="sm" variant="outline" onClick={() => runTest(m.name)} disabled={testMailbox.isPending}>
                    {testMailbox.isPending && testMailbox.variables === m.name ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <PlugZap className="h-3.5 w-3.5" />
                    )}
                    测试连接
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => setEditingName(m.name)}>
                    <Pencil className="h-3.5 w-3.5" /> 编辑
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="ml-auto text-destructive hover:bg-destructive/10 hover:text-destructive"
                    disabled={RESERVED.has(m.name)}
                    title={RESERVED.has(m.name) ? '内置邮箱不能删除' : undefined}
                    onClick={() => {
                      if (window.confirm(`删除邮箱「${m.name}」？今日已发送的邮箱将无法删除（后端会拦截）。`)) {
                        deleteMailbox.mutate(m.name)
                      }
                    }}
                  >
                    <Trash2 className="h-3.5 w-3.5" /> 删除
                  </Button>
                </div>
              ) : null}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  type = 'text',
  hint,
  inputMode,
}: {
  label: string
  value: string
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void
  placeholder?: string
  type?: string
  hint?: string
  inputMode?: 'numeric' | 'text' | 'email'
}) {
  return (
    <div className="flex flex-col gap-1">
      <label className="text-xs text-muted-foreground">{label}</label>
      <Input value={value} onChange={onChange} placeholder={placeholder} type={type} inputMode={inputMode} />
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  )
}
