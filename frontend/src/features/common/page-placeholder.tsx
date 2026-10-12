import { Construction } from 'lucide-react'

/** 里程碑占位页 —— 尚未实现的模块展示友好提示而不是空白。 */
export function PagePlaceholder({
  title,
  milestone,
  description,
}: {
  title: string
  milestone: string
  description: string
}) {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center rounded-xl border border-dashed p-10 text-center">
      <Construction className="h-10 w-10 text-muted-foreground/50" />
      <h1 className="mt-4 text-xl font-semibold">{title}</h1>
      <p className="mt-2 max-w-md text-sm text-muted-foreground">{description}</p>
      <span className="mt-4 rounded-full bg-accent px-3 py-1 text-xs font-medium text-muted-foreground">
        {milestone} 里程碑启用
      </span>
    </div>
  )
}
