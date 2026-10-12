/**
 * 中文映射 — 把后端英文枚举转成客户端展示中文。
 * 与 FTA 数据模型枚举保持一致（leads.status / communications.direction|status）。
 */

export const LEAD_STATUS_ZH: Record<string, string> = {
  new: '未联系',
  contacted: '已联系',
  replied: '已回复',
  customer: '成交客户',
  dead: '已失效',
}

export const COMM_DIRECTION_ZH: Record<string, string> = {
  outbound: '发出',
  inbound: '收到',
}

export const COMM_STATUS_ZH: Record<string, string> = {
  pending: '待发送',
  sent: '已发送',
  delivered: '已送达',
  failed: '失败',
  bounced: '退信',
  replied: '已回复',
  received: '已收到',
}

export const STATUS_BADGE_VARIANT: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  new: 'secondary',
  contacted: 'default',
  replied: 'outline',
  customer: 'default',
  dead: 'destructive',
}

/** 来源分类：后端的 source 值可能很多，给高频来源一个友好的中文名。 */
export const SOURCE_ZH: Record<string, string> = {
  xunkexing: '寻客星',
  facebook: 'Facebook',
  linkedin: 'LinkedIn',
  zoho: '手动跟进',
  manual: '手动录入',
  'DM自主搜寻数据': '官网邮箱',
  '孚盟数据': '孚盟(高质量)',
  '孚盟数据普通': '孚盟(普通)',
  industry_site: '行业目录站',
}

export function sourceZh(source: string | null | undefined): string {
  if (!source) return '未知'
  return SOURCE_ZH[source] ?? source
}

/** 邮箱展示名 —— 与后端 mailbox 配置对应。 */
export const MAILBOX_ZH: Record<string, string> = {
  default: 'NetEase 163',
  zoho: 'Zoho 个人邮箱',
}
