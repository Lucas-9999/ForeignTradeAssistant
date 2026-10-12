import { cn } from '@/lib/utils'

/** 标签/分组预设色板 —— 与仪表盘图表 PALETTE 风格一致。 */
export const COLOR_PRESETS = [
  '#2563eb', '#10b981', '#f59e0b', '#ef4444',
  '#8b5cf6', '#06b6d4', '#ec4899', '#84cc16',
  '#64748b', '#f97316',
]

export function ColorSwatches({ value, onChange }: { value: string; onChange: (c: string) => void }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {COLOR_PRESETS.map((c) => (
        <button
          key={c}
          type="button"
          onClick={() => onChange(c)}
          className={cn(
            'h-6 w-6 rounded-full border-2 transition-transform hover:scale-110',
            value === c ? 'border-foreground ring-2 ring-offset-1' : 'border-transparent',
          )}
          style={{ background: c }}
          aria-label={`颜色 ${c}`}
        />
      ))}
    </div>
  )
}
