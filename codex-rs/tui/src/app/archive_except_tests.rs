use super::archive_except_failure_with_progress;
use super::validated_next_root;
use codex_protocol::ThreadId;
use codex_state::ArchiveExceptPlan;
use codex_state::ArchiveExceptSubtree;
use pretty_assertions::assert_eq;
use std::collections::HashSet;

fn thread_id(value: u128) -> ThreadId {
    ThreadId::from_u128(value)
}

#[test]
fn validation_rejects_candidate_expansion_after_confirmation() {
    let keep = thread_id(1);
    let confirmed = thread_id(2);
    let expanded = thread_id(3);
    let plan = ArchiveExceptPlan {
        keep_thread_id: keep,
        protected_thread_ids: vec![keep],
        candidate_thread_ids: vec![confirmed, expanded],
        subtrees: vec![
            ArchiveExceptSubtree {
                root_thread_id: confirmed,
                thread_ids: vec![confirmed],
            },
            ArchiveExceptSubtree {
                root_thread_id: expanded,
                thread_ids: vec![expanded],
            },
        ],
    };

    assert_eq!(
        validated_next_root(&plan, &HashSet::from([confirmed])).unwrap_err(),
        "the active candidate set expanded after confirmation; run /archive-except again"
    );
}

#[test]
fn validation_rejects_a_subtree_that_intersects_the_protected_family() {
    let keep = thread_id(10);
    let candidate = thread_id(11);
    let plan = ArchiveExceptPlan {
        keep_thread_id: keep,
        protected_thread_ids: vec![keep],
        candidate_thread_ids: vec![candidate],
        subtrees: vec![ArchiveExceptSubtree {
            root_thread_id: candidate,
            thread_ids: vec![candidate, keep],
        }],
    };

    assert_eq!(
        validated_next_root(&plan, &HashSet::from([candidate])).unwrap_err(),
        "a replanned archive subtree is not safely contained in the confirmed set"
    );
}

#[test]
fn partial_failure_message_reports_exact_observed_progress() {
    assert_eq!(
        archive_except_failure_with_progress(5, 2, "the third subtree failed"),
        "Archive-except stopped: 2 of 5 confirmed sessions are currently archived; the third subtree failed."
    );
}
