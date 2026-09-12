use super::App;
use crate::AppServerTarget;
use crate::app_server_session::AppServerSession;
use codex_rollout::StateDbHandle;
use codex_state::ArchiveExceptPlan;
use codex_state::ArchiveExceptSubtree;
use std::collections::HashSet;

impl App {
    pub(super) async fn prepare_archive_except(&mut self) {
        let (keep_thread_id, state_db) = match self.archive_except_context() {
            Ok(context) => context,
            Err(message) => {
                self.chat_widget.add_error_message(message);
                return;
            }
        };
        match state_db.plan_archive_except(keep_thread_id).await {
            Ok(plan) if plan.candidate_thread_ids.is_empty() => self.chat_widget.add_info_message(
                "No other active local sessions were found across projects or working directories."
                    .to_string(),
                /*hint*/ None,
            ),
            Ok(plan) => self.chat_widget.show_archive_except_confirmation(plan),
            Err(error) => self
                .chat_widget
                .add_error_message(format!("Failed to plan archive-except safely: {error}")),
        }
    }

    pub(super) async fn confirm_archive_except(
        &mut self,
        app_server: &mut AppServerSession,
        confirmed: ArchiveExceptPlan,
    ) {
        let confirmed_ids = confirmed
            .candidate_thread_ids
            .iter()
            .copied()
            .collect::<HashSet<_>>();
        let mut last_archived_root = None;
        loop {
            let (keep_thread_id, state_db) = match self.archive_except_context() {
                Ok(context) if context.0 == confirmed.keep_thread_id => context,
                Ok(_) => {
                    self.report_archive_except_failure(
                        &confirmed,
                        "the displayed session changed after confirmation",
                    )
                    .await;
                    return;
                }
                Err(message) => {
                    self.report_archive_except_failure(&confirmed, &message)
                        .await;
                    return;
                }
            };
            let current = match state_db.plan_archive_except(keep_thread_id).await {
                Ok(plan) => plan,
                Err(error) => {
                    self.report_archive_except_failure(
                        &confirmed,
                        &format!("the thread graph could not be replanned safely: {error}"),
                    )
                    .await;
                    return;
                }
            };
            if let Some(root) = last_archived_root
                && current.candidate_thread_ids.contains(&root)
            {
                self.report_archive_except_failure(
                    &confirmed,
                    "the archive server reported success without removing the selected root",
                )
                .await;
                return;
            }
            let next_root = match validated_next_root(&current, &confirmed_ids) {
                Ok(next_root) => next_root,
                Err(message) => {
                    self.report_archive_except_failure(&confirmed, &message)
                        .await;
                    return;
                }
            };
            let Some(root_thread_id) = next_root else {
                self.report_archive_except_success(&confirmed).await;
                return;
            };
            if let Err(error) = app_server.thread_archive(root_thread_id).await {
                self.report_archive_except_failure(
                    &confirmed,
                    &format!("archiving subtree {root_thread_id} failed: {error}"),
                )
                .await;
                return;
            }
            last_archived_root = Some(root_thread_id);
        }
    }

    fn archive_except_context(&self) -> Result<(codex_protocol::ThreadId, StateDbHandle), String> {
        if !matches!(self.app_server_target, AppServerTarget::Embedded) {
            return Err("'/archive-except' is available only for local embedded sessions.".into());
        }
        if self.chat_widget.task_is_running() {
            return Err("'/archive-except' is disabled while a task is in progress.".into());
        }
        let keep_thread_id = self.current_displayed_thread_id().ok_or_else(|| {
            "A thread must start before other sessions can be archived.".to_string()
        })?;
        if self.side_threads.contains_key(&keep_thread_id) {
            return Err(
                "'/archive-except' is unavailable in side conversations. Press Ctrl+C to return to the main thread first."
                    .into(),
            );
        }
        let state_db = self.state_db.clone().ok_or_else(|| {
            "'/archive-except' requires the local thread state database.".to_string()
        })?;
        Ok((keep_thread_id, state_db))
    }

    async fn report_archive_except_failure(&mut self, confirmed: &ArchiveExceptPlan, reason: &str) {
        let message =
            archive_except_progress_message(self.state_db.as_ref(), confirmed, reason).await;
        self.chat_widget.add_error_message(message);
    }

    async fn report_archive_except_success(&mut self, confirmed: &ArchiveExceptPlan) {
        let total = confirmed.candidate_thread_ids.len();
        let archived = match self.state_db.as_ref() {
            Some(state_db) => {
                state_db
                    .count_archived_threads(&confirmed.candidate_thread_ids)
                    .await
            }
            None => {
                self.chat_widget.add_error_message(
                    "Archive roots completed, but exact progress cannot be verified because local state is missing."
                        .to_string(),
                );
                return;
            }
        };
        match archived {
            Ok(archived) => self.chat_widget.add_info_message(
                format!(
                    "Archive-except completed: {archived} of {total} confirmed sessions are archived across all projects and working directories. The current session remains active."
                ),
                /*hint*/ None,
            ),
            Err(error) => self.chat_widget.add_error_message(format!(
                "Archive roots completed, but exact progress could not be read from local state: {error}"
            )),
        }
    }
}

fn validated_next_root(
    current: &ArchiveExceptPlan,
    confirmed_ids: &HashSet<codex_protocol::ThreadId>,
) -> Result<Option<codex_protocol::ThreadId>, String> {
    let candidates = current
        .candidate_thread_ids
        .iter()
        .copied()
        .collect::<HashSet<_>>();
    if !candidates.is_subset(confirmed_ids) {
        return Err(
            "the active candidate set expanded after confirmation; run /archive-except again"
                .into(),
        );
    }
    let protected = current
        .protected_thread_ids
        .iter()
        .copied()
        .collect::<HashSet<_>>();
    let mut covered = HashSet::new();
    for subtree in &current.subtrees {
        validate_subtree(subtree, &candidates, &protected, confirmed_ids)?;
        covered.extend(subtree.thread_ids.iter().copied());
    }
    if covered != candidates {
        return Err("the replanned candidate subtrees did not cover the candidate set".into());
    }
    Ok(current
        .subtrees
        .first()
        .map(|subtree| subtree.root_thread_id))
}

fn validate_subtree(
    subtree: &ArchiveExceptSubtree,
    candidates: &HashSet<codex_protocol::ThreadId>,
    protected: &HashSet<codex_protocol::ThreadId>,
    confirmed: &HashSet<codex_protocol::ThreadId>,
) -> Result<(), String> {
    let closure = subtree.thread_ids.iter().copied().collect::<HashSet<_>>();
    if closure.is_empty()
        || !closure.contains(&subtree.root_thread_id)
        || !closure.is_subset(candidates)
        || !closure.is_subset(confirmed)
        || !closure.is_disjoint(protected)
    {
        return Err(
            "a replanned archive subtree is not safely contained in the confirmed set".into(),
        );
    }
    Ok(())
}

async fn archive_except_progress_message(
    state_db: Option<&StateDbHandle>,
    confirmed: &ArchiveExceptPlan,
    reason: &str,
) -> String {
    let total = confirmed.candidate_thread_ids.len();
    match state_db {
        Some(state_db) => match state_db
            .count_archived_threads(&confirmed.candidate_thread_ids)
            .await
        {
            Ok(archived) => archive_except_failure_with_progress(total, archived, reason),
            Err(error) => format!(
                "Archive-except stopped, but exact progress could not be read from local state ({error}); {reason}."
            ),
        },
        None => format!(
            "Archive-except stopped, but exact progress is unavailable because local state is missing; {reason}."
        ),
    }
}

fn archive_except_failure_with_progress(total: usize, archived: usize, reason: &str) -> String {
    format!(
        "Archive-except stopped: {archived} of {total} confirmed sessions are currently archived; {reason}."
    )
}

#[cfg(test)]
#[path = "archive_except_tests.rs"]
mod tests;
