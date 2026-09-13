use super::StateRuntime;
use crate::PINNED_THREAD_SECTION_ID;
use codex_protocol::ThreadId;
use sqlx::QueryBuilder;
use sqlx::Sqlite;

mod graph;
mod model;
mod planner;
mod subtrees;

use graph::ArchiveExceptGraph;
use graph::EdgeRecord;
use graph::ThreadRecord;
pub use model::ArchiveExceptGroup;
pub use model::ArchiveExceptGroupDisposition;
pub use model::ArchiveExceptLimit;
pub use model::ArchiveExceptPlan;
pub use model::ArchiveExceptProtectionReason;
pub use model::ArchiveExceptRequest;
pub use model::ArchiveExceptSubtree;

impl StateRuntime {
    /// Build a point-in-time plan of complete session groups safe to archive.
    pub async fn plan_archive_except(
        &self,
        request: ArchiveExceptRequest,
    ) -> anyhow::Result<ArchiveExceptPlan> {
        let (threads, edges) = self.load_archive_except_snapshot().await?;
        ArchiveExceptGraph::from_rows(threads, edges)?.plan(request)
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

    async fn load_archive_except_snapshot(
        &self,
    ) -> anyhow::Result<(Vec<ThreadRecord>, Vec<EdgeRecord>)> {
        let mut transaction = self.pool.begin().await?;
        let thread_rows = sqlx::query_as::<_, (String, String, bool, bool, i64)>(
            "SELECT id, source, archived, thread_section_id = ?, \
             COALESCE(created_at_ms, 0) FROM threads ORDER BY id",
        )
        .bind(PINNED_THREAD_SECTION_ID)
        .fetch_all(&mut *transaction)
        .await?;
        let edge_rows = sqlx::query_as::<_, (String, String, String)>(
            "SELECT parent_thread_id, child_thread_id, status FROM thread_spawn_edges \
             ORDER BY parent_thread_id, child_thread_id, status",
        )
        .fetch_all(&mut *transaction)
        .await?;
        transaction.commit().await?;
        Ok((
            thread_rows
                .into_iter()
                .map(ThreadRecord::try_from)
                .collect::<anyhow::Result<_>>()?,
            edge_rows
                .into_iter()
                .map(EdgeRecord::try_from)
                .collect::<anyhow::Result<_>>()?,
        ))
    }
}

#[cfg(test)]
#[path = "archive_except_tests.rs"]
mod tests;
