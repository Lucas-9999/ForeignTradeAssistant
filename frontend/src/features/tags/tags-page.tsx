import { useState } from 'react'
import { Plus, Pencil, Trash2, Check } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { ColorSwatches } from '@/components/ui/color-swatches'
import { useTags, useCreateTag, useUpdateTag, useDeleteTag } from '@/hooks/use-tags-groups'
import { PagePlaceholder } from '@/features/common/page-placeholder'
import { formatCount } from '@/lib/utils'
import type { TagRow } from '@/services/tags-groups'

/** 标签管理 —— M4：新建 / 重命名 / 换色 / 删除，彩色卡片网格。 */
export function TagsPage() {
  const { data, isLoading, isError } = useTags()
  const createTag = useCreateTag()
  const updateTag = useUpdateTag()
  const deleteTag = useDeleteTag()

  // 新建表单
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [newCategory, setNewCategory] = useState('custom')
  const [newColor, setNewColor] = useState('#2563eb')

  // 编辑态（单卡片）
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editName, setEditName] = useState('')
  const [editCategory, setEditCategory] = useState('')
  const [editColor, setEditColor] = useState('')

  const mutationError = createTag.error ?? updateTag.error ?? deleteTag.error

  const submitCreate = () => {
    const name = newName.trim()
    if (!name) return
    createTag.mutate(
      { name, category: newCategory || 'custom', color: newColor },
      { onSuccess: () => { setNewName(''); setNewCategory('custom'); setCreating(false) } },
    )
  }

  const startEdit = (t: TagRow) => {
    setEditingId(t.id)
    setEditName(t.name)
    setEditCategory(t.category)
    setEditColor(t.color)
  }

  const submitEdit = () => {
    const name = editName.trim()
    if (!name || editingId == null) return
    updateTag.mutate(
      { id: editingId, data: { name, category: editCategory, color: editColor } },
      { onSuccess: () => setEditingId(null) },
    )
  }

  const confirmDelete = (t: TagRow) => {
    if (window.confirm(`删除标签「${t.name}」？${t.lead_count} 条线索上的该标签将被移除（线索本身不受影响）。`)) {
      deleteTag.mutate(t.id)
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
    return <PagePlaceholder title="标签" milestone="M4" description="标签数据加载失败，请确认后端 :8000 已启动。" />
  }

  if (!data.available) {
    return (
      <PagePlaceholder
        title="标签"
        milestone="M4"
        description="标签功能需要数据库 V7 迁移（tags / lead_tags 表）——重启后端后自动生效。"
      />
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">标签</h1>
          <p className="text-sm text-muted-foreground">共 {formatCount(data.tags.length)} 个标签，用于给客户线索打标分类</p>
        </div>
        <Button size="sm" onClick={() => setCreating((v) => !v)}>
          <Plus className="h-4 w-4" /> 新建标签
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
                <label className="text-xs text-muted-foreground">标签名 *</label>
                <Input
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder="如：VIP 客户"
                  autoFocus
                />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-xs text-muted-foreground">类别</label>
                <Input value={newCategory} onChange={(e) => setNewCategory(e.target.value)} placeholder="custom" />
              </div>
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-xs text-muted-foreground">颜色</label>
              <ColorSwatches value={newColor} onChange={setNewColor} />
            </div>
            <div className="flex items-center gap-2">
              <Button size="sm" onClick={submitCreate} disabled={!newName.trim() || createTag.isPending}>
                <Check className="h-4 w-4" /> 创建
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setCreating(false)}>
                取消
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {/* 标签网格 */}
      {data.tags.length === 0 ? (
        <div className="rounded-lg border border-dashed p-10 text-center text-sm text-muted-foreground">
          还没有标签。点击右上角「新建标签」开始给客户分类。
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {data.tags.map((t) =>
            editingId === t.id ? (
              <Card key={t.id}>
                <CardContent className="space-y-3 pt-4">
                  <Input value={editName} onChange={(e) => setEditName(e.target.value)} autoFocus />
                  <Input value={editCategory} onChange={(e) => setEditCategory(e.target.value)} placeholder="类别" />
                  <ColorSwatches value={editColor} onChange={setEditColor} />
                  <div className="flex items-center gap-2">
                    <Button size="sm" onClick={submitEdit} disabled={!editName.trim() || updateTag.isPending}>
                      <Check className="h-4 w-4" /> 保存
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setEditingId(null)}>
                      取消
                    </Button>
                  </div>
                </CardContent>
              </Card>
            ) : (
              <Card key={t.id}>
                <CardHeader>
                  <CardTitle className="flex items-center justify-between text-base">
                    <span className="flex items-center gap-2">
                      <span className="inline-block h-3 w-3 shrink-0 rounded-full" style={{ background: t.color }} />
                      <span className="truncate">{t.name}</span>
                    </span>
                    <span className="flex items-center gap-0.5">
                      <button type="button" onClick={() => startEdit(t)} className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground" aria-label="编辑">
                        <Pencil className="h-3.5 w-3.5" />
                      </button>
                      <button type="button" onClick={() => confirmDelete(t)} className="rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive" aria-label="删除">
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </span>
                  </CardTitle>
                </CardHeader>
                <CardContent className="flex items-center justify-between text-sm">
                  <Badge variant="outline">{t.category || 'custom'}</Badge>
                  <span className="text-muted-foreground">{formatCount(t.lead_count)} 条线索</span>
                </CardContent>
              </Card>
            ),
          )}
        </div>
      )}
    </div>
  )
}
