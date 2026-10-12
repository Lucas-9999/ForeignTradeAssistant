import { get, post, put, del } from './api-client'

export interface TagRow {
  id: number
  name: string
  category: string
  color: string
  created_at: string | null
  lead_count: number
}

export interface GroupRow {
  id: number
  name: string
  description: string | null
  color: string
  created_at: string | null
  member_count: number
}

export interface TagsResponse {
  available: boolean
  tags: TagRow[]
}

export interface GroupsResponse {
  available: boolean
  groups: GroupRow[]
}

export interface TagInput {
  name: string
  category?: string
  color?: string
}

export interface GroupInput {
  name: string
  description?: string
  color?: string
}

export interface LeadIdsRequest {
  lead_ids: number[]
}

export function fetchTags(signal?: AbortSignal): Promise<TagsResponse> {
  return get<TagsResponse>('/api/v1/tags', undefined, { signal })
}

export function fetchGroups(signal?: AbortSignal): Promise<GroupsResponse> {
  return get<GroupsResponse>('/api/v1/groups', undefined, { signal })
}

// ── Tags 写操作 (M4) ──────────────────────────────────────────────────────

export function createTag(data: TagInput): Promise<TagRow> {
  return post<TagRow>('/api/v1/tags', data)
}

export function updateTag(id: number, data: Partial<TagInput>): Promise<TagRow> {
  return put<TagRow>(`/api/v1/tags/${id}`, data)
}

export function deleteTag(id: number): Promise<{ deleted: boolean; tag_id: number }> {
  return del(`/api/v1/tags/${id}`)
}

export function addTagLeads(id: number, leadIds: number[]): Promise<{ tag_id: number; assigned: number }> {
  return post(`/api/v1/tags/${id}/leads`, { lead_ids: leadIds })
}

export function removeTagLeads(id: number, leadIds: number[]): Promise<{ tag_id: number; removed: number }> {
  return del(`/api/v1/tags/${id}/leads`, { lead_ids: leadIds })
}

// ── Groups 写操作 (M4) ────────────────────────────────────────────────────

export function createGroup(data: GroupInput): Promise<GroupRow> {
  return post<GroupRow>('/api/v1/groups', data)
}

export function updateGroup(id: number, data: Partial<GroupInput>): Promise<GroupRow> {
  return put<GroupRow>(`/api/v1/groups/${id}`, data)
}

export function deleteGroup(id: number): Promise<{ deleted: boolean; group_id: number }> {
  return del(`/api/v1/groups/${id}`)
}

export function addGroupLeads(id: number, leadIds: number[]): Promise<{ group_id: number; assigned: number }> {
  return post(`/api/v1/groups/${id}/leads`, { lead_ids: leadIds })
}

export function removeGroupLeads(id: number, leadIds: number[]): Promise<{ group_id: number; removed: number }> {
  return del(`/api/v1/groups/${id}/leads`, { lead_ids: leadIds })
}
