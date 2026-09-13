use super::graph::ArchiveExceptGraph;
use super::model::ArchiveExceptSubtree;
use codex_protocol::ThreadId;
use std::collections::HashSet;

impl ArchiveExceptGraph {
    pub(super) fn archive_subtrees(&self, targets: &[ThreadId]) -> Vec<ArchiveExceptSubtree> {
        let target_set = targets.iter().copied().collect::<HashSet<_>>();
        targets
            .iter()
            .filter(|id| !self.has_target_ancestor(**id, &target_set))
            .map(|root| ArchiveExceptSubtree {
                root_thread_id: *root,
                thread_ids: targets
                    .iter()
                    .filter(|id| self.descends_from(**id, *root))
                    .copied()
                    .collect(),
            })
            .collect()
    }

    fn has_target_ancestor(&self, thread_id: ThreadId, targets: &HashSet<ThreadId>) -> bool {
        let mut seen = HashSet::new();
        let mut current = self.parent_by_child.get(&thread_id).copied();
        while let Some(ancestor) = current {
            if targets.contains(&ancestor) {
                return true;
            }
            if !seen.insert(ancestor) {
                return true;
            }
            current = self.parent_by_child.get(&ancestor).copied();
        }
        false
    }

    fn descends_from(&self, mut thread_id: ThreadId, root: ThreadId) -> bool {
        let mut seen = HashSet::new();
        while seen.insert(thread_id) {
            if thread_id == root {
                return true;
            }
            let Some(parent) = self.parent_by_child.get(&thread_id) else {
                return false;
            };
            thread_id = *parent;
        }
        false
    }
}
