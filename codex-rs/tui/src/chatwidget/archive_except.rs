use super::ChatWidget;
use crate::app_event::AppEvent;
use crate::bottom_pane::SelectionItem;
use crate::bottom_pane::SelectionViewParams;
use codex_state::ArchiveExceptGroup;
use codex_state::ArchiveExceptGroupDisposition;
use codex_state::ArchiveExceptPlan;
use codex_state::ArchiveExceptProtectionReason;

const PREVIEW_DETAIL_GROUP_LIMIT: usize = 50;

impl ChatWidget {
    pub(crate) fn show_archive_except_confirmation(&mut self, plan: ArchiveExceptPlan) {
        let summary = ArchiveExceptSummary::from_plan(&plan);
        let candidate_count = plan.candidate_thread_ids.len();
        self.bottom_pane.show_selection_view(SelectionViewParams {
            title: Some(format!(
                "Archive {candidate_count} sessions in {} groups?",
                summary.candidate_groups
            )),
            subtitle: Some(format!(
                "Global across projects and working directories. {} protected; {} limit-skipped. The current loaded session group is kept.",
                summary.protected_groups, summary.limit_skipped_groups
            )),
            items: vec![
                SelectionItem {
                    name: "No, keep all sessions".to_string(),
                    description: Some("Cancel without archiving".to_string()),
                    is_default: true,
                    dismiss_on_select: true,
                    ..Default::default()
                },
                SelectionItem {
                    name: format!("Yes, archive {candidate_count} sessions"),
                    description: Some(format!(
                        "Archive {} complete groups using the embedded app server",
                        summary.candidate_groups
                    )),
                    actions: vec![Box::new(move |tx| {
                        tx.send(AppEvent::ConfirmArchiveExcept(plan.clone()));
                    })],
                    require_explicit_confirmation: true,
                    dismiss_on_select: true,
                    ..Default::default()
                },
            ],
            ..Default::default()
        });
        self.request_redraw();
    }

    pub(crate) fn show_archive_except_preview(&mut self, plan: ArchiveExceptPlan) {
        let summary = ArchiveExceptSummary::from_plan(&plan);
        let mut items = vec![SelectionItem {
            name: "Close preview".to_string(),
            description: Some("No sessions will be archived".to_string()),
            is_default: true,
            dismiss_on_select: true,
            ..Default::default()
        }];
        items.extend(
            plan.groups
                .iter()
                .take(PREVIEW_DETAIL_GROUP_LIMIT)
                .map(group_preview_item),
        );
        if plan.groups.len() > PREVIEW_DETAIL_GROUP_LIMIT {
            items.push(SelectionItem {
                name: format!(
                    "{} additional groups",
                    plan.groups.len() - PREVIEW_DETAIL_GROUP_LIMIT
                ),
                description: Some("Omitted from detail; included in totals above".to_string()),
                is_disabled: true,
                disabled_gutter_marker: Some("·"),
                ..Default::default()
            });
        }
        self.bottom_pane.show_selection_view(SelectionViewParams {
            title: Some("Archive-except preview".to_string()),
            subtitle: Some(format!(
                "{} groups; {} eligible ({} sessions); {} protected; {} limit-skipped; {} already archived.",
                plan.groups.len(),
                summary.candidate_groups,
                plan.candidate_thread_ids.len(),
                summary.protected_groups,
                summary.limit_skipped_groups,
                summary.archived_groups
            )),
            items,
            ..Default::default()
        });
        self.request_redraw();
    }

    pub(crate) fn show_archive_except_empty(&mut self, plan: ArchiveExceptPlan) {
        let summary = ArchiveExceptSummary::from_plan(&plan);
        self.add_info_message(
            format!(
                "Archive-except found no eligible sessions across projects or working directories: {} protected groups, {} limit-skipped groups, {} already archived groups.",
                summary.protected_groups,
                summary.limit_skipped_groups,
                summary.archived_groups
            ),
            /*hint*/ None,
        );
    }

    pub(crate) fn task_is_running(&self) -> bool {
        self.bottom_pane.is_task_running()
    }
}

#[derive(Default)]
struct ArchiveExceptSummary {
    candidate_groups: usize,
    protected_groups: usize,
    limit_skipped_groups: usize,
    archived_groups: usize,
}

impl ArchiveExceptSummary {
    fn from_plan(plan: &ArchiveExceptPlan) -> Self {
        let mut summary = Self::default();
        for group in &plan.groups {
            match group.disposition {
                ArchiveExceptGroupDisposition::Candidate => summary.candidate_groups += 1,
                ArchiveExceptGroupDisposition::Protected => summary.protected_groups += 1,
                ArchiveExceptGroupDisposition::LimitSkipped => summary.limit_skipped_groups += 1,
                ArchiveExceptGroupDisposition::AlreadyArchived => summary.archived_groups += 1,
            }
        }
        summary
    }
}

fn group_preview_item(group: &ArchiveExceptGroup) -> SelectionItem {
    let status = match group.disposition {
        ArchiveExceptGroupDisposition::Candidate => "eligible".to_string(),
        ArchiveExceptGroupDisposition::AlreadyArchived => "archived".to_string(),
        ArchiveExceptGroupDisposition::LimitSkipped => "limit-skipped".to_string(),
        ArchiveExceptGroupDisposition::Protected => format!(
            "protected:{}",
            group
                .protection_reasons
                .iter()
                .map(protection_reason_label)
                .collect::<Vec<_>>()
                .join(",")
        ),
    };
    SelectionItem {
        name: format!("root={}", group.root_thread_id),
        description: Some(format!(
            "members={} targets={} status={status}",
            group.member_thread_ids.len(),
            group.target_thread_ids.len()
        )),
        is_disabled: true,
        disabled_gutter_marker: Some("·"),
        ..Default::default()
    }
}

fn protection_reason_label(reason: &ArchiveExceptProtectionReason) -> &'static str {
    match reason {
        ArchiveExceptProtectionReason::Loaded => "loaded",
        ArchiveExceptProtectionReason::Pinned => "pinned",
        ArchiveExceptProtectionReason::EdgeNotClosed => "edge-not-closed",
        ArchiveExceptProtectionReason::OrphanSubagent => "orphan-subagent",
        ArchiveExceptProtectionReason::MissingParent => "missing-parent",
        ArchiveExceptProtectionReason::AmbiguousRelation => "ambiguous-relation",
        ArchiveExceptProtectionReason::Cycle => "cycle",
    }
}
