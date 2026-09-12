use super::StateRuntime;
use anyhow::bail;
use codex_protocol::ThreadId;
use sqlx::QueryBuilder;
use sqlx::Sqlite;
use std::collections::HashMap;
use std::collections::HashSet;

/// One independently archivable candidate subtree.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ArchiveExceptSubtree {
    pub root_thread_id: ThreadId,
    pub thread_ids: Vec<ThreadId>,
}

/// A point-in-time, fail-closed plan for archiving every thread except one family.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ArchiveExceptPlan {
    pub keep_thread_id: ThreadId,
    pub protected_thread_ids: Vec<ThreadId>,
    pub candidate_thread_ids: Vec<ThreadId>,
    pub subtrees: Vec<ArchiveExceptSubtree>,
}

struct ThreadGraph {
    archived_by_id: HashMap<ThreadId, bool>,
    parent_by_child: HashMap<ThreadId, ThreadId>,
    children_by_parent: HashMap<ThreadId, Vec<ThreadId>>,
}

impl StateRuntime {
    /// Plan a global local archive while protecting the keep thread, its ancestors, and descendants.
    pub async fn plan_archive_except(
        &self,
        keep_thread_id: ThreadId,
    ) -> anyhow::Result<ArchiveExceptPlan> {
        self.load_thread_graph().await?.plan(keep_thread_id)
    }

    /// Count which confirmed thread ids are archived in the current database state.
    pub async fn count_archived_threads(&self, thread_ids: &[ThreadId]) -> anyhow::Result<usize> {
        if thread_ids.is_empty() {
            return Ok(0);
        }
        let mut query = QueryBuilder::<Sqlite>::new(
            "SELECT COUNT(*) FROM threads WHERE archived = 1 AND id IN (",
        );
        let mut separated = query.separated(", ");
        for thread_id in thread_ids {
            separated.push_bind(thread_id.to_string());
        }
        separated.push_unseparated(")");
        let (count,) = query
            .build_query_as::<(i64,)>()
            .fetch_one(self.pool.as_ref())
            .await?;
        Ok(usize::try_from(count)?)
    }

    async fn load_thread_graph(&self) -> anyhow::Result<ThreadGraph> {
        let mut transaction = self.pool.begin().await?;
        let thread_rows =
            sqlx::query_as::<_, (String, bool)>("SELECT id, archived FROM threads ORDER BY id")
                .fetch_all(&mut *transaction)
                .await?;
        let edge_rows = sqlx::query_as::<_, (String, String)>(
            "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges \
             ORDER BY parent_thread_id, child_thread_id",
        )
        .fetch_all(&mut *transaction)
        .await?;
        transaction.commit().await?;
        ThreadGraph::from_rows(thread_rows, edge_rows)
    }
}

impl ThreadGraph {
    fn from_rows(
        thread_rows: Vec<(String, bool)>,
        edge_rows: Vec<(String, String)>,
    ) -> anyhow::Result<Self> {
        let archived_by_id = thread_rows
            .into_iter()
            .map(|(id, archived)| Ok((ThreadId::try_from(id)?, archived)))
            .collect::<anyhow::Result<HashMap<_, _>>>()?;
        let mut parent_by_child = HashMap::new();
        let mut children_by_parent = HashMap::<ThreadId, Vec<ThreadId>>::new();
        for (parent, child) in edge_rows {
            let parent = ThreadId::try_from(parent)?;
            let child = ThreadId::try_from(child)?;
            if !archived_by_id.contains_key(&parent) || !archived_by_id.contains_key(&child) {
                bail!("archive-except cannot safely plan a thread graph with missing nodes");
            }
            if parent_by_child.insert(child, parent).is_some() {
                bail!("archive-except cannot safely plan a thread graph with multiple parents");
            }
            children_by_parent.entry(parent).or_default().push(child);
        }
        let graph = Self {
            archived_by_id,
            parent_by_child,
            children_by_parent,
        };
        graph.validate_acyclic()?;
        Ok(graph)
    }

    fn plan(&self, keep_thread_id: ThreadId) -> anyhow::Result<ArchiveExceptPlan> {
        match self.archived_by_id.get(&keep_thread_id) {
            None => bail!("archive-except keep thread {keep_thread_id} was not found"),
            Some(true) => bail!("archive-except keep thread {keep_thread_id} is already archived"),
            Some(false) => {}
        }
        let protected = self.protected_family(keep_thread_id);
        let candidates = self
            .archived_by_id
            .iter()
            .filter_map(|(id, archived)| (!archived && !protected.contains(id)).then_some(*id))
            .collect::<HashSet<_>>();
        let subtrees = self.candidate_subtrees(&candidates, &protected)?;
        let mut protected_thread_ids = protected.into_iter().collect::<Vec<_>>();
        sort_thread_ids(&mut protected_thread_ids);
        let mut candidate_thread_ids = candidates.into_iter().collect::<Vec<_>>();
        sort_thread_ids(&mut candidate_thread_ids);
        Ok(ArchiveExceptPlan {
            keep_thread_id,
            protected_thread_ids,
            candidate_thread_ids,
            subtrees,
        })
    }

    fn protected_family(&self, keep_thread_id: ThreadId) -> HashSet<ThreadId> {
        let mut protected = HashSet::from([keep_thread_id]);
        let mut ancestor = keep_thread_id;
        while let Some(parent) = self.parent_by_child.get(&ancestor) {
            protected.insert(*parent);
            ancestor = *parent;
        }
        let mut pending = vec![keep_thread_id];
        while let Some(parent) = pending.pop() {
            for child in self.children_by_parent.get(&parent).into_iter().flatten() {
                if protected.insert(*child) {
                    pending.push(*child);
                }
            }
        }
        protected
    }

    fn candidate_subtrees(
        &self,
        candidates: &HashSet<ThreadId>,
        protected: &HashSet<ThreadId>,
    ) -> anyhow::Result<Vec<ArchiveExceptSubtree>> {
        let mut roots = candidates
            .iter()
            .filter(|candidate| !self.has_candidate_ancestor(**candidate, candidates))
            .copied()
            .collect::<Vec<_>>();
        sort_thread_ids(&mut roots);
        let mut covered = HashSet::new();
        let mut subtrees = Vec::new();
        for root_thread_id in roots {
            let thread_ids = self.active_descendants_including_root(root_thread_id);
            if thread_ids.iter().any(|id| protected.contains(id))
                || thread_ids.iter().any(|id| !candidates.contains(id))
            {
                bail!("archive-except candidate subtree intersects the protected thread family");
            }
            covered.extend(thread_ids.iter().copied());
            subtrees.push(ArchiveExceptSubtree {
                root_thread_id,
                thread_ids,
            });
        }
        if covered != *candidates {
            bail!("archive-except could not safely partition every candidate thread");
        }
        Ok(subtrees)
    }

    fn has_candidate_ancestor(
        &self,
        mut thread_id: ThreadId,
        candidates: &HashSet<ThreadId>,
    ) -> bool {
        while let Some(parent) = self.parent_by_child.get(&thread_id) {
            if candidates.contains(parent) {
                return true;
            }
            thread_id = *parent;
        }
        false
    }

    fn active_descendants_including_root(&self, root: ThreadId) -> Vec<ThreadId> {
        let mut active = HashSet::new();
        let mut pending = vec![root];
        while let Some(thread_id) = pending.pop() {
            if !self.archived_by_id.get(&thread_id).copied().unwrap_or(true) {
                active.insert(thread_id);
            }
            pending.extend(
                self.children_by_parent
                    .get(&thread_id)
                    .into_iter()
                    .flatten()
                    .copied(),
            );
        }
        let mut active = active.into_iter().collect::<Vec<_>>();
        sort_thread_ids(&mut active);
        active
    }

    fn validate_acyclic(&self) -> anyhow::Result<()> {
        for start in self.archived_by_id.keys() {
            let mut path = HashSet::new();
            let mut current = *start;
            while path.insert(current) {
                let Some(parent) = self.parent_by_child.get(&current) else {
                    break;
                };
                current = *parent;
            }
            if self.parent_by_child.contains_key(&current) {
                bail!("archive-except cannot safely plan a cyclic thread graph");
            }
        }
        Ok(())
    }
}

fn sort_thread_ids(thread_ids: &mut [ThreadId]) {
    thread_ids.sort_unstable_by_key(ToString::to_string);
}

#[cfg(test)]
#[path = "archive_except_tests.rs"]
mod tests;
