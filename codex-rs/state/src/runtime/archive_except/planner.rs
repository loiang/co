use super::graph::ArchiveExceptGraph;
use super::graph::thread_id_cmp;
use super::model::ArchiveExceptGroup;
use super::model::ArchiveExceptGroupDisposition;
use super::model::ArchiveExceptLimit;
use super::model::ArchiveExceptPlan;
use super::model::ArchiveExceptProtectionReason;
use super::model::ArchiveExceptRequest;
use anyhow::bail;
use codex_protocol::ThreadId;
use std::cmp::Ordering;
use std::collections::HashSet;

impl ArchiveExceptGraph {
    pub(super) fn plan(
        &self,
        mut request: ArchiveExceptRequest,
    ) -> anyhow::Result<ArchiveExceptPlan> {
        match self.records.get(&request.keep_thread_id) {
            None => bail!(
                "archive-except keep thread {} was not found",
                request.keep_thread_id
            ),
            Some(record) if record.archived => bail!(
                "archive-except keep thread {} is already archived",
                request.keep_thread_id
            ),
            Some(_) => {}
        }
        request.loaded_thread_ids.insert(request.keep_thread_id);
        let mut groups = self
            .components()
            .into_iter()
            .map(|component| self.make_group(&component, &request.loaded_thread_ids))
            .collect::<Vec<_>>();
        groups.sort_by(|left, right| {
            left.created_at_ms
                .cmp(&right.created_at_ms)
                .then_with(|| thread_id_cmp(&left.root_thread_id, &right.root_thread_id))
        });
        apply_limit(&mut groups, request.limit);
        Ok(make_plan(request.keep_thread_id, groups))
    }

    fn make_group(
        &self,
        component: &HashSet<ThreadId>,
        loaded: &HashSet<ThreadId>,
    ) -> ArchiveExceptGroup {
        let root_thread_id = self.choose_root(component);
        let member_thread_ids = self.ordered_members(root_thread_id, component);
        let target_thread_ids = member_thread_ids
            .iter()
            .filter(|id| !self.records[id].archived)
            .copied()
            .collect::<Vec<_>>();
        ArchiveExceptGroup {
            root_thread_id,
            subtrees: self.archive_subtrees(&target_thread_ids),
            protection_reasons: self.protection_reasons(component, root_thread_id, loaded),
            created_at_ms: self.records[&root_thread_id].created_at_ms,
            member_thread_ids,
            target_thread_ids,
            disposition: ArchiveExceptGroupDisposition::AlreadyArchived,
        }
    }

    fn choose_root(&self, component: &HashSet<ThreadId>) -> ThreadId {
        component
            .iter()
            .filter(|id| {
                self.parent_by_child
                    .get(id)
                    .is_none_or(|parent| !component.contains(parent))
            })
            .min_by(|left, right| self.record_cmp(left, right))
            .or_else(|| {
                component
                    .iter()
                    .min_by(|left, right| self.record_cmp(left, right))
            })
            .copied()
            .expect("components are non-empty")
    }

    fn ordered_members(&self, root: ThreadId, component: &HashSet<ThreadId>) -> Vec<ThreadId> {
        let mut ordered = Vec::new();
        let mut seen = HashSet::new();
        let mut pending = vec![root];
        while let Some(thread_id) = pending.pop() {
            if !seen.insert(thread_id) {
                continue;
            }
            ordered.push(thread_id);
            let mut children = self
                .children_by_parent
                .get(&thread_id)
                .map_or_else(Vec::new, |ids| {
                    ids.intersection(component).copied().collect::<Vec<_>>()
                });
            children.sort_by(|left, right| self.record_cmp(left, right));
            pending.extend(children.into_iter().rev());
        }
        let mut unseen = component.difference(&seen).copied().collect::<Vec<_>>();
        unseen.sort_by(|left, right| self.record_cmp(left, right));
        ordered.extend(unseen);
        ordered
    }

    fn protection_reasons(
        &self,
        component: &HashSet<ThreadId>,
        root: ThreadId,
        loaded: &HashSet<ThreadId>,
    ) -> Vec<ArchiveExceptProtectionReason> {
        use ArchiveExceptProtectionReason as Reason;
        let checks = [
            (
                component.iter().any(|id| loaded.contains(id)),
                Reason::Loaded,
            ),
            (
                component.iter().any(|id| self.records[id].pinned),
                Reason::Pinned,
            ),
            (
                !component.is_disjoint(&self.edge_not_closed),
                Reason::EdgeNotClosed,
            ),
            (self.is_orphan_subagent(root), Reason::OrphanSubagent),
            (
                !component.is_disjoint(&self.missing_parent),
                Reason::MissingParent,
            ),
            (
                !component.is_disjoint(&self.ambiguous),
                Reason::AmbiguousRelation,
            ),
            (self.has_cycle(component), Reason::Cycle),
        ];
        checks
            .into_iter()
            .filter_map(|(applies, reason)| applies.then_some(reason))
            .collect()
    }

    fn is_orphan_subagent(&self, root: ThreadId) -> bool {
        !self.parent_by_child.contains_key(&root)
            && !self.missing_parent.contains(&root)
            && self.records[&root]
                .source
                .to_lowercase()
                .replace('_', "")
                .contains("subagent")
    }

    fn has_cycle(&self, component: &HashSet<ThreadId>) -> bool {
        component.iter().any(|start| {
            let mut seen = HashSet::new();
            let mut current = Some(*start);
            while let Some(thread_id) = current.filter(|id| component.contains(id)) {
                if !seen.insert(thread_id) {
                    return true;
                }
                current = self.parent_by_child.get(&thread_id).copied();
            }
            false
        })
    }

    fn record_cmp(&self, left: &ThreadId, right: &ThreadId) -> Ordering {
        self.records[left]
            .created_at_ms
            .cmp(&self.records[right].created_at_ms)
            .then_with(|| thread_id_cmp(left, right))
    }
}

fn apply_limit(groups: &mut [ArchiveExceptGroup], limit: ArchiveExceptLimit) {
    let mut remaining = match limit {
        ArchiveExceptLimit::Unlimited => None,
        ArchiveExceptLimit::MaxThreads(limit) => Some(limit.get()),
    };
    for group in groups {
        group.disposition = if !group.protection_reasons.is_empty() {
            ArchiveExceptGroupDisposition::Protected
        } else if group.target_thread_ids.is_empty() {
            ArchiveExceptGroupDisposition::AlreadyArchived
        } else if remaining.is_some_and(|count| group.target_thread_ids.len() > count) {
            ArchiveExceptGroupDisposition::LimitSkipped
        } else {
            if let Some(count) = &mut remaining {
                *count -= group.target_thread_ids.len();
            }
            ArchiveExceptGroupDisposition::Candidate
        };
    }
}

fn make_plan(keep_thread_id: ThreadId, groups: Vec<ArchiveExceptGroup>) -> ArchiveExceptPlan {
    let protected_thread_ids = groups
        .iter()
        .filter(|group| group.disposition == ArchiveExceptGroupDisposition::Protected)
        .flat_map(|group| group.member_thread_ids.iter().copied())
        .collect();
    let candidate_thread_ids = groups
        .iter()
        .filter(|group| group.disposition == ArchiveExceptGroupDisposition::Candidate)
        .flat_map(|group| group.target_thread_ids.iter().copied())
        .collect();
    let subtrees = groups
        .iter()
        .filter(|group| group.disposition == ArchiveExceptGroupDisposition::Candidate)
        .flat_map(|group| group.subtrees.iter().cloned())
        .collect();
    ArchiveExceptPlan {
        keep_thread_id,
        protected_thread_ids,
        candidate_thread_ids,
        subtrees,
        groups,
    }
}
