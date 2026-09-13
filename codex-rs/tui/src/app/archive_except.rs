use super::App;
use super::archive_except_loaded::load_all_loaded_thread_ids;
use crate::AppServerTarget;
use crate::app_server_session::AppServerSession;
use crate::archive_except::ArchiveExceptMode;
use crate::archive_except::ArchiveExceptOptions;
use codex_protocol::ThreadId;
use codex_rollout::StateDbHandle;
use codex_state::ArchiveExceptGroup;
use codex_state::ArchiveExceptGroupDisposition;
use codex_state::ArchiveExceptPlan;
use codex_state::ArchiveExceptRequest;
use codex_state::ArchiveExceptSubtree;

impl App {
    pub(super) async fn prepare_archive_except(
        &mut self,
        app_server: &mut AppServerSession,
        options: ArchiveExceptOptions,
    ) {
        let (keep_thread_id, _) = match self.archive_except_context() {
            Ok(context) => context,
            Err(message) => {
                self.chat_widget.add_error_message(message);
                return;
            }
        };
        let loaded = match load_all_loaded_thread_ids(app_server).await {
            Ok(loaded) => loaded,
            Err(error) => {
                self.chat_widget
                    .add_error_message(format!("Failed to plan archive-except safely: {error}"));
                return;
            }
        };
        let (current_thread_id, state_db) = match self.archive_except_context() {
            Ok(context) if context.0 == keep_thread_id => context,
            Ok(_) => {
                self.chat_widget.add_error_message(
                    "Failed to plan archive-except safely: the displayed session changed while loaded sessions were queried."
                        .to_string(),
                );
                return;
            }
            Err(message) => {
                self.chat_widget.add_error_message(message);
                return;
            }
        };
        let mut request = ArchiveExceptRequest::new(current_thread_id, loaded);
        if let Some(limit) = options.limit {
            request = request.with_limit(limit);
        }
        match state_db.plan_archive_except(request).await {
            Ok(plan) if options.mode == ArchiveExceptMode::Preview => {
                self.chat_widget.show_archive_except_preview(plan);
            }
            Ok(plan) if plan.candidate_thread_ids.is_empty() => {
                self.chat_widget.show_archive_except_empty(plan);
            }
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
        let confirmed_groups = confirmed
            .groups
            .iter()
            .filter(|group| group.disposition == ArchiveExceptGroupDisposition::Candidate)
            .cloned()
            .collect::<Vec<_>>();
        for group in &confirmed_groups {
            let mut remaining_subtrees = group.subtrees.clone();
            while !remaining_subtrees.is_empty() {
                let fresh = match self
                    .refresh_archive_except_plan(app_server, confirmed.keep_thread_id)
                    .await
                {
                    Ok(plan) => plan,
                    Err(reason) => {
                        self.report_archive_except_failure(&confirmed, &reason)
                            .await;
                        return;
                    }
                };
                let subtree = match validated_next_subtree(&fresh, group, &remaining_subtrees) {
                    Ok(subtree) => subtree,
                    Err(reason) => {
                        self.report_archive_except_failure(&confirmed, &reason)
                            .await;
                        return;
                    }
                };
                if let Err(error) = app_server.thread_archive(subtree.root_thread_id).await {
                    self.report_archive_except_failure(
                        &confirmed,
                        &format!(
                            "archiving subtree {} failed: {error}",
                            subtree.root_thread_id
                        ),
                    )
                    .await;
                    return;
                }
                if let Err(reason) = self.verify_archive_except_subtree(&subtree).await {
                    self.report_archive_except_failure(&confirmed, &reason)
                        .await;
                    return;
                }
                remaining_subtrees.remove(0);
            }
        }
        self.report_archive_except_success(&confirmed, confirmed_groups.len())
            .await;
    }

    async fn refresh_archive_except_plan(
        &self,
        app_server: &mut AppServerSession,
        confirmed_keep: ThreadId,
    ) -> Result<ArchiveExceptPlan, String> {
        let (keep_thread_id, _) = self.archive_except_context()?;
        if keep_thread_id != confirmed_keep {
            return Err("the displayed session changed after confirmation".to_string());
        }
        let loaded = load_all_loaded_thread_ids(app_server).await?;
        let (current_thread_id, state_db) = self.archive_except_context()?;
        if current_thread_id != confirmed_keep {
            return Err("the displayed session changed while loaded sessions were queried".into());
        }
        state_db
            .plan_archive_except(ArchiveExceptRequest::new(current_thread_id, loaded))
            .await
            .map_err(|error| format!("the thread graph could not be replanned safely: {error}"))
    }

    fn archive_except_context(&self) -> Result<(ThreadId, StateDbHandle), String> {
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

    async fn verify_archive_except_subtree(
        &self,
        subtree: &ArchiveExceptSubtree,
    ) -> Result<(), String> {
        let state_db = self
            .state_db
            .as_ref()
            .ok_or_else(|| "local state disappeared after archiving".to_string())?;
        let archived = state_db
            .count_archived_threads(&subtree.thread_ids)
            .await
            .map_err(|error| format!("the archive result could not be verified: {error}"))?;
        if archived != subtree.thread_ids.len() {
            return Err(format!(
                "the archive server did not archive the complete subtree rooted at {}",
                subtree.root_thread_id
            ));
        }
        Ok(())
    }

    async fn report_archive_except_failure(&mut self, confirmed: &ArchiveExceptPlan, reason: &str) {
        let message =
            archive_except_progress_message(self.state_db.as_ref(), confirmed, reason).await;
        self.chat_widget.add_error_message(message);
    }

    async fn report_archive_except_success(
        &mut self,
        confirmed: &ArchiveExceptPlan,
        groups: usize,
    ) {
        let total = confirmed.candidate_thread_ids.len();
        let archived = match count_confirmed_archived(self.state_db.as_ref(), confirmed).await {
            Ok(archived) => archived,
            Err(message) => {
                self.chat_widget.add_error_message(message);
                return;
            }
        };
        if archived != total {
            self.chat_widget.add_error_message(format!(
                "Archive-except stopped: {archived} of {total} confirmed sessions are archived after all archive roots completed."
            ));
            return;
        }
        self.chat_widget.add_info_message(
            format!(
                "Archive-except completed: {archived} sessions in {groups} groups archived across all projects and working directories. The current session remains active."
            ),
            /*hint*/ None,
        );
    }
}

fn validated_next_subtree(
    fresh: &ArchiveExceptPlan,
    confirmed: &ArchiveExceptGroup,
    remaining_subtrees: &[ArchiveExceptSubtree],
) -> Result<ArchiveExceptSubtree, String> {
    let current = fresh
        .group_for(confirmed.root_thread_id)
        .ok_or_else(|| "a confirmed archive group disappeared during execution".to_string())?;
    if current.member_thread_ids != confirmed.member_thread_ids {
        return Err("a confirmed archive group changed membership during execution".to_string());
    }
    if current.disposition != ArchiveExceptGroupDisposition::Candidate {
        return Err(format!(
            "a confirmed archive group became unsafe during execution: {:?}",
            current.protection_reasons
        ));
    }
    let remaining_targets = remaining_subtrees
        .iter()
        .flat_map(|subtree| subtree.thread_ids.iter().copied())
        .collect::<Vec<_>>();
    if current.target_thread_ids != remaining_targets || current.subtrees != remaining_subtrees {
        return Err(
            "a confirmed archive group changed targets or relationships during execution"
                .to_string(),
        );
    }
    current
        .subtrees
        .first()
        .cloned()
        .ok_or_else(|| "a confirmed candidate group has no archive root".to_string())
}

async fn count_confirmed_archived(
    state_db: Option<&StateDbHandle>,
    confirmed: &ArchiveExceptPlan,
) -> Result<usize, String> {
    let state_db = state_db.ok_or_else(|| {
        "Archive roots completed, but exact progress is unavailable because local state is missing."
            .to_string()
    })?;
    state_db
        .count_archived_threads(&confirmed.candidate_thread_ids)
        .await
        .map_err(|error| {
            format!("Archive roots completed, but exact progress could not be read: {error}")
        })
}

async fn archive_except_progress_message(
    state_db: Option<&StateDbHandle>,
    confirmed: &ArchiveExceptPlan,
    reason: &str,
) -> String {
    let total = confirmed.candidate_thread_ids.len();
    match count_confirmed_archived(state_db, confirmed).await {
        Ok(archived) => archive_except_failure_with_progress(total, archived, reason),
        Err(error) => format!("Archive-except stopped; {error} Reason: {reason}."),
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
