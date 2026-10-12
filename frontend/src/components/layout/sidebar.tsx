import { NavLink } from 'react-router-dom'
import {
  LayoutDashboard,
  Users,
  Tags as TagsIcon,
  FolderOpen,
  Mail,
  MailPlus,
  ShieldCheck,
  Wrench,
} from 'lucide-react'
import { cn } from '@/lib/utils'

const NAV = [
  { to: '/', label: '仪表盘', icon: LayoutDashboard, end: true },
  { to: '/leads', label: '客户线索', icon: Users },
  { to: '/tags', label: '标签', icon: TagsIcon },
  { to: '/groups', label: '分组', icon: FolderOpen },
  { to: '/mailboxes', label: '邮箱管理', icon: Mail },
  { to: '/email-center', label: '邮件中心', icon: MailPlus, disabled: false },
  { to: '/reputation', label: '投递信誉', icon: ShieldCheck },
]

export function Sidebar() {
  return (
    <aside className="flex h-full w-56 flex-col border-r bg-card">
      <div className="flex h-14 items-center gap-2 border-b px-4">
        <Wrench className="h-5 w-5 text-primary" />
        <div className="leading-tight">
          <div className="text-sm font-semibold">FTA 外贸助理</div>
          <div className="text-[11px] text-muted-foreground">CRM 管理控制台</div>
        </div>
      </div>
      <nav className="flex-1 space-y-1 p-2">
        {NAV.map((item) =>
          item.disabled ? (
            <div
              key={item.to}
              title="M6 里程碑启用"
              className="flex cursor-not-allowed items-center gap-3 rounded-md px-3 py-2 text-sm text-muted-foreground/50"
            >
              <item.icon className="h-4 w-4" />
              {item.label}
              <span className="ml-auto text-[10px] text-muted-foreground/40">M6</span>
            </div>
          ) : (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors',
                  isActive
                    ? 'bg-primary/10 text-primary'
                    : 'text-muted-foreground hover:bg-accent hover:text-foreground',
                )
              }
            >
              <item.icon className="h-4 w-4" />
              {item.label}
            </NavLink>
          ),
        )}
      </nav>
      <div className="border-t p-3 text-[11px] text-muted-foreground">
        Streamlit 旧控制台
        <br />
        <span className="text-muted-foreground/60">ui_review.py :8501</span>
      </div>
    </aside>
  )
}
