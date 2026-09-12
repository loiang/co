use super::ChatWidget;
use crate::app_event::AppEvent;
use crate::bottom_pane::SelectionItem;
use crate::bottom_pane::SelectionViewParams;
use codex_state::ArchiveExceptPlan;

impl ChatWidget {
    pub(crate) fn show_archive_except_confirmation(&mut self, plan: ArchiveExceptPlan) {
        let candidate_count = plan.candidate_thread_ids.len();
        self.bottom_pane.show_selection_view(SelectionViewParams {
            title: Some(format!("Archive {candidate_count} other local sessions?")),
            subtitle: Some(
                "Global across projects; keeps this session, ancestors, and descendants."
                    .to_string(),
            ),
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
                    description: Some("Includes every local working directory".to_string()),
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

    pub(crate) fn task_is_running(&self) -> bool {
        self.bottom_pane.is_task_running()
    }
}
