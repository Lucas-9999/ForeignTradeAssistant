import { Route, Routes } from 'react-router-dom'
import { AppShell } from '@/components/layout/app-shell'
import { DashboardPage } from '@/features/dashboard/dashboard-page'
import { LeadsPage } from '@/features/leads/leads-page'
import { TagsPage } from '@/features/tags/tags-page'
import { GroupsPage } from '@/features/groups/groups-page'
import { MailboxesPage } from '@/features/mailboxes/mailboxes-page'
import { EmailCenterPage } from '@/features/email-center/email-center-page'
import { ReputationPage } from '@/features/reputation/reputation-page'

export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<DashboardPage />} />
        <Route path="leads" element={<LeadsPage />} />
        <Route path="tags" element={<TagsPage />} />
        <Route path="groups" element={<GroupsPage />} />
        <Route path="mailboxes" element={<MailboxesPage />} />
        <Route path="email-center" element={<EmailCenterPage />} />
        <Route path="reputation" element={<ReputationPage />} />
        <Route path="*" element={<DashboardPage />} />
      </Route>
    </Routes>
  )
}
