import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  fetchTags,
  fetchGroups,
  createTag,
  updateTag,
  deleteTag,
  addTagLeads,
  removeTagLeads,
  createGroup,
  updateGroup,
  deleteGroup,
  addGroupLeads,
  removeGroupLeads,
  type TagInput,
  type GroupInput,
} from '@/services/tags-groups'

/** 标签/分组任一写操作后，标签列表、分组列表、线索列表（携带 tags[]/groups[]）都要刷新。 */
function invalidateTagGroupCaches(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: ['tags'] })
  qc.invalidateQueries({ queryKey: ['groups'] })
  qc.invalidateQueries({ queryKey: ['leads'] })
}

export function useTags() {
  return useQuery({
    queryKey: ['tags'],
    queryFn: ({ signal }) => fetchTags(signal),
    staleTime: 60_000,
  })
}

export function useGroups() {
  return useQuery({
    queryKey: ['groups'],
    queryFn: ({ signal }) => fetchGroups(signal),
    staleTime: 60_000,
  })
}

// ── Tags ───────────────────────────────────────────────────────────────────

export function useCreateTag() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data: TagInput) => createTag(data),
    onSuccess: () => invalidateTagGroupCaches(qc),
  })
}

export function useUpdateTag() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<TagInput> }) => updateTag(id, data),
    onSuccess: () => invalidateTagGroupCaches(qc),
  })
}

export function useDeleteTag() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => deleteTag(id),
    onSuccess: () => invalidateTagGroupCaches(qc),
  })
}

export function useAddTagLeads() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, leadIds }: { id: number; leadIds: number[] }) => addTagLeads(id, leadIds),
    onSuccess: () => invalidateTagGroupCaches(qc),
  })
}

export function useRemoveTagLeads() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, leadIds }: { id: number; leadIds: number[] }) => removeTagLeads(id, leadIds),
    onSuccess: () => invalidateTagGroupCaches(qc),
  })
}

// ── Groups ─────────────────────────────────────────────────────────────────

export function useCreateGroup() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data: GroupInput) => createGroup(data),
    onSuccess: () => invalidateTagGroupCaches(qc),
  })
}

export function useUpdateGroup() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<GroupInput> }) => updateGroup(id, data),
    onSuccess: () => invalidateTagGroupCaches(qc),
  })
}

export function useDeleteGroup() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => deleteGroup(id),
    onSuccess: () => invalidateTagGroupCaches(qc),
  })
}

export function useAddGroupLeads() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, leadIds }: { id: number; leadIds: number[] }) => addGroupLeads(id, leadIds),
    onSuccess: () => invalidateTagGroupCaches(qc),
  })
}

export function useRemoveGroupLeads() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, leadIds }: { id: number; leadIds: number[] }) => removeGroupLeads(id, leadIds),
    onSuccess: () => invalidateTagGroupCaches(qc),
  })
}
