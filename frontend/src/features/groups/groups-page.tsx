import { useState } from 'react'
import { Plus, Pencil, Trash2, Check, Users } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { ColorSwatches } from '@/components/ui/color-swatches'
import { useGroups, useCreateGroup, useUpdateGroup, useDeleteGroup } from '@/hooks/use-tags-groups'
import { PagePlaceholder } from '@/features/common/page-placeholder'
import { formatCount } from '@/lib/utils'
import type { GroupRow } from '@/services/tags-groups'

/** 分组管理 —— M4：新建 / 改名 / 描述 / 删除，成员数展示。 */
export function GroupsPage() {
  const { data, isLoading, isError } = useGroups()
  const createGroup = useCreateGroup()
  const updateGroup = useUpdateGroup()
  const deleteGroup = useDeleteGroup()

  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const [newColor, setNewColor] = useState('#10b981')

  const [editingId, setEditingId] = useState<number | null>(null)
  const [editName, setEditName] = useState('')
  const [editDesc, setEditDesc] = useState('')
  const [editColor, setEditColor] = useState('')

  const mutationError = createGroup.error ?? updateGroup.error ?? deleteGroup.error

  const submitCreate = () => {
    const name = newName.trim()
    if (!name) return
    createGroup.mutate(
      { name, description: newDesc.trim() || undefined, color: newColor },
      { onSuccess: () => { setNewName(''); setNewDesc(''); setCreating(false) } },
    )
  }

  const startEdit = (g: GroupRow) => {
    setEditingId(g.id)
    setEditName(g.name)
    setEditDesc(g.description ?? '')
    setEditColor(g.color)
  }

  const submitEdit = () => {
    const name = editName.trim()
    if (!name || editingId == null) return
    updateGroup.mutate(
      { id: editingId, data: { name, description: editDesc.trim() || undefined, color: editColor } },
      { onSuccess: () => setEditingId(null) },
    )
  }

  const confirmDelete = (g: GroupRow) => {
    if (window.confirm(`删除分组「${g.name}」？${g.member_count} 条线索将从该分组移除（线索本身不受影响）。`)) {
      deleteGroup.mutate(g.id)
    }
  }

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-40" />
        <Skeleton className="h-64 rounded-xl" />
      </div>
    )
  }

  if (isError || !data) {
    return <PagePlaceholder title="分组" milestone="M4" description="分组数据加载失败，请确认后端 :8000 已启动。" />
  }

  if (!data.available) {
    return (
      <PagePlaceholder
        title="分组"
        milestone="M4"
        description="分组功能需要数据库 V7 迁移（groups / group_members 表）——重启后端后自动生效。"
      />
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">分组</h1>
          <p className="text-sm text-muted-foreground">共 {formatCount(data.groups.length)} 个分组，把线索归入命名客户池</p>
        </div>
        <Button size="sm" onClick={() => setCreating((v) => !v)}>
          <Plus className="h-4 w-4" /> 新建分组
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
          <CardContent className="space-y-3 pt-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="flex flex-col gap-1">
                <label className="text-xs text-muted-foreground">分组名 *</label>
                <Input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="如：德国重点客户" autoFocus />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-xs text-muted-foreground">描述</label>
                <Input value={newDesc} onChange={(e) => setNewDesc(e.target.value)} placeholder="可选" />
              </div>
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-xs text-muted-foreground">颜色</label>
              <ColorSwatches value={newColor} onChange={setNewColor} />
            </div>
            <div className="flex items-center gap-2">
              <Button size="sm" onClick={submitCreate} disabled={!newName.trim() || createGroup.isPending}>
                <Check className="h-4 w-4" /> 创建
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setCreating(false)}>
                取消
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {/* 分组网格 */}
      {data.groups.length === 0 ? (
        <div className="rounded-lg border border-dashed p-10 text-center text-sm text-muted-foreground">
          还没有分组。点击右上角「新建分组」建立客户池。
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {data.groups.map((g) =>
            editingId === g.id ? (
              <Card key={g.id}>
                <CardContent className="space-y-3 pt-4">
                  <Input value={editName} onChange={(e) => setEditName(e.target.value)} autoFocus />
                  <Input value={editDesc} onChange={(e) => setEditDesc(e.target.value)} placeholder="描述" />
                  <ColorSwatches value={editColor} onChange={setEditColor} />
                  <div className="flex items-center gap-2">
                    <Button size="sm" onClick={submitEdit} disabled={!editName.trim() || updateGroup.isPending}>
                      <Check className="h-4 w-4" /> 保存
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setEditingId(null)}>
                      取消
                    </Button>
                  </div>
                </CardContent>
              </Card>
            ) : (
              <Card key={g.id}>
                <CardHeader>
                  <CardTitle className="flex items-center justify-between text-base">
                    <span className="flex items-center gap-2">
                      <span className="inline-block h-3 w-3 shrink-0 rounded-full" style={{ background: g.color }} />
                      <span className="truncate">{g.name}</span>
                    </span>
                    <span className="flex items-center gap-0.5">
                      <button type="button" onClick={() => startEdit(g)} className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground" aria-label="编辑">
                        <Pencil className="h-3.5 w-3.5" />
                      </button>
                      <button type="button" onClick={() => confirmDelete(g)} className="rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive" aria-label="删除">
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </span>
                  </CardTitle>
                  {g.description ? <CardDescription>{g.description}</CardDescription> : null}
                </CardHeader>
                <CardContent className="flex items-center gap-1.5 text-sm text-muted-foreground">
                  <Users className="h-4 w-4" />
                  {formatCount(g.member_count)} 条线索
                </CardContent>
              </Card>
            ),
          )}
        </div>
      )}
    </div>
  )
}
