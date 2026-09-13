use super::archive_except_failure_with_progress;
use super::validated_next_subtree;
use codex_protocol::ThreadId;
use codex_state::ArchiveExceptGroup;
use codex_state::ArchiveExceptGroupDisposition;
use codex_state::ArchiveExceptPlan;
use codex_state::ArchiveExceptProtectionReason;
use codex_state::ArchiveExceptSubtree;
use pretty_assertions::assert_eq;

fn thread_id(value: u128) -> ThreadId {
    ThreadId::from_u128(value)
}

fn group(root: ThreadId, members: Vec<ThreadId>) -> ArchiveExceptGroup {
    let subtree = ArchiveExceptSubtree {
        root_thread_id: root,
        thread_ids: members.clone(),
    };
    ArchiveExceptGroup {
        root_thread_id: root,
        member_thread_ids: members.clone(),
        target_thread_ids: members,
        subtrees: vec![subtree],
        protection_reasons: Vec::new(),
        created_at_ms: 0,
        disposition: ArchiveExceptGroupDisposition::Candidate,
    }
}

fn plan(keep: ThreadId, groups: Vec<ArchiveExceptGroup>) -> ArchiveExceptPlan {
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
        keep_thread_id: keep,
        protected_thread_ids: vec![keep],
        candidate_thread_ids,
        subtrees,
        groups,
    }
}

#[test]
fn validation_rejects_new_descendant_in_confirmed_group() {
    let keep = thread_id(1);
    let root = thread_id(2);
    let confirmed = group(root, vec![root]);
    let expanded = group(root, vec![root, thread_id(3)]);

    assert_eq!(
        validated_next_subtree(&plan(keep, vec![expanded]), &confirmed, &confirmed.subtrees)
            .unwrap_err(),
        "a confirmed archive group changed membership during execution"
    );
}

#[test]
fn validation_rejects_group_that_becomes_loaded() {
    let keep = thread_id(10);
    let root = thread_id(11);
    let confirmed = group(root, vec![root]);
    let mut loaded = confirmed.clone();
    loaded.disposition = ArchiveExceptGroupDisposition::Protected;
    loaded.protection_reasons = vec![ArchiveExceptProtectionReason::Loaded];

    assert_eq!(
        validated_next_subtree(&plan(keep, vec![loaded]), &confirmed, &confirmed.subtrees)
            .unwrap_err(),
        "a confirmed archive group became unsafe during execution: [Loaded]"
    );
}

#[test]
fn validation_ignores_unconfirmed_limit_replacement_group() {
    let keep = thread_id(20);
    let confirmed_root = thread_id(21);
    let replacement_root = thread_id(22);
    let confirmed = group(confirmed_root, vec![confirmed_root]);
    let replacement = group(replacement_root, vec![replacement_root]);

    assert_eq!(
        validated_next_subtree(
            &plan(keep, vec![replacement, confirmed.clone()]),
            &confirmed,
            &confirmed.subtrees,
        ),
        Ok(confirmed.subtrees[0].clone())
    );
}

#[test]
fn validation_rejects_relationship_change_with_same_members() {
    let keep = thread_id(30);
    let root = thread_id(31);
    let child = thread_id(32);
    let confirmed = group(root, vec![root, child]);
    let mut changed = confirmed.clone();
    changed.subtrees = vec![
        ArchiveExceptSubtree {
            root_thread_id: root,
            thread_ids: vec![root],
        },
        ArchiveExceptSubtree {
            root_thread_id: child,
            thread_ids: vec![child],
        },
    ];

    assert_eq!(
        validated_next_subtree(&plan(keep, vec![changed]), &confirmed, &confirmed.subtrees)
            .unwrap_err(),
        "a confirmed archive group changed targets or relationships during execution"
    );
}

#[test]
fn partial_failure_message_reports_exact_observed_progress() {
    assert_eq!(
        archive_except_failure_with_progress(5, 2, "the third subtree failed"),
        "Archive-except stopped: 2 of 5 confirmed sessions are currently archived; the third subtree failed."
    );
}
