use super::ArchiveExceptGroupDisposition;
use super::ArchiveExceptLimit;
use super::ArchiveExceptPlan;
use super::ArchiveExceptProtectionReason;
use super::ArchiveExceptRequest;
use super::ArchiveExceptSubtree;
use super::StateRuntime;
use crate::DirectionalThreadSpawnEdgeStatus;
use crate::PINNED_THREAD_SECTION_ID;
use crate::runtime::test_support::test_thread_metadata;
use crate::runtime::test_support::unique_temp_dir;
use anyhow::Result;
use chrono::Utc;
use codex_protocol::ThreadId;
use codex_utils_absolute_path::test_support::PathExt;
use pretty_assertions::assert_eq;
use std::collections::HashSet;
use std::num::NonZeroUsize;
use uuid::Uuid;

fn thread_id(value: u128) -> ThreadId {
    ThreadId::from_string(&Uuid::from_u128(value).to_string()).expect("valid thread id")
}

async fn runtime_with_threads(thread_ids: &[ThreadId]) -> Result<std::sync::Arc<StateRuntime>> {
    let codex_home = unique_temp_dir();
    let runtime = StateRuntime::init(
        crate::SqliteConfig::new_for_testing(codex_home.as_path().abs()),
        "test-provider".to_string(),
    )
    .await?;
    for (created_at_ms, &id) in thread_ids.iter().enumerate() {
        let mut metadata = test_thread_metadata(&codex_home, id, codex_home.clone());
        metadata.created_at =
            chrono::DateTime::from_timestamp_millis(created_at_ms as i64).expect("valid timestamp");
        runtime.upsert_thread(&metadata).await?;
        sqlx::query("UPDATE threads SET created_at_ms = ? WHERE id = ?")
            .bind(created_at_ms as i64)
            .bind(id.to_string())
            .execute(runtime.pool.as_ref())
            .await?;
    }
    Ok(runtime)
}

async fn edge(
    runtime: &StateRuntime,
    parent: ThreadId,
    child: ThreadId,
    status: DirectionalThreadSpawnEdgeStatus,
) -> Result<()> {
    runtime
        .upsert_thread_spawn_edge(parent, child, status)
        .await
}

fn request(keep_thread_id: ThreadId, loaded: &[ThreadId]) -> ArchiveExceptRequest {
    ArchiveExceptRequest::new(keep_thread_id, loaded.iter().copied().collect())
}

#[tokio::test]
async fn keep_descendant_protects_the_whole_component_including_siblings() -> Result<()> {
    let root = thread_id(1);
    let keep = thread_id(2);
    let sibling = thread_id(3);
    let sibling_child = thread_id(4);
    let other_root = thread_id(5);
    let runtime = runtime_with_threads(&[root, keep, sibling, sibling_child, other_root]).await?;
    for (parent, child) in [(root, keep), (root, sibling), (sibling, sibling_child)] {
        edge(
            &runtime,
            parent,
            child,
            DirectionalThreadSpawnEdgeStatus::Closed,
        )
        .await?;
    }

    let plan = runtime.plan_archive_except(request(keep, &[])).await?;

    assert_eq!(
        plan.protected_thread_ids,
        vec![root, keep, sibling, sibling_child]
    );
    assert_eq!(plan.candidate_thread_ids, vec![other_root]);
    assert_eq!(
        plan.groups[0].protection_reasons,
        vec![ArchiveExceptProtectionReason::Loaded]
    );
    assert_eq!(
        plan.groups[0].disposition,
        ArchiveExceptGroupDisposition::Protected
    );
    Ok(())
}

#[tokio::test]
async fn archived_intermediate_keeps_group_connected_and_pinned_descendant_protects_it()
-> Result<()> {
    let keep = thread_id(10);
    let root = thread_id(11);
    let archived_middle = thread_id(12);
    let pinned = thread_id(13);
    let sibling = thread_id(14);
    let runtime = runtime_with_threads(&[keep, root, archived_middle, pinned, sibling]).await?;
    for (parent, child) in [
        (root, archived_middle),
        (archived_middle, pinned),
        (root, sibling),
    ] {
        edge(
            &runtime,
            parent,
            child,
            DirectionalThreadSpawnEdgeStatus::Closed,
        )
        .await?;
    }
    mark_archived(&runtime, archived_middle).await?;
    sqlx::query("UPDATE threads SET thread_section_id = ? WHERE id = ?")
        .bind(PINNED_THREAD_SECTION_ID)
        .bind(pinned.to_string())
        .execute(runtime.pool.as_ref())
        .await?;

    let plan = runtime.plan_archive_except(request(keep, &[])).await?;
    let group = plan.group_for(root).expect("root group");

    assert_eq!(
        group.member_thread_ids,
        vec![root, archived_middle, pinned, sibling]
    );
    assert_eq!(group.target_thread_ids, vec![root, pinned, sibling]);
    assert_eq!(
        group.protection_reasons,
        vec![ArchiveExceptProtectionReason::Pinned]
    );
    assert_eq!(group.disposition, ArchiveExceptGroupDisposition::Protected);
    Ok(())
}

#[tokio::test]
async fn loaded_descendant_and_open_edge_each_protect_their_whole_group() -> Result<()> {
    let keep = thread_id(20);
    let loaded_root = thread_id(21);
    let loaded_child = thread_id(22);
    let loaded_sibling = thread_id(23);
    let open_root = thread_id(24);
    let open_child = thread_id(25);
    let runtime = runtime_with_threads(&[
        keep,
        loaded_root,
        loaded_child,
        loaded_sibling,
        open_root,
        open_child,
    ])
    .await?;
    for (parent, child, status) in [
        (
            loaded_root,
            loaded_child,
            DirectionalThreadSpawnEdgeStatus::Closed,
        ),
        (
            loaded_root,
            loaded_sibling,
            DirectionalThreadSpawnEdgeStatus::Closed,
        ),
        (
            open_root,
            open_child,
            DirectionalThreadSpawnEdgeStatus::Open,
        ),
    ] {
        edge(&runtime, parent, child, status).await?;
    }

    let plan = runtime
        .plan_archive_except(request(keep, &[loaded_child]))
        .await?;

    assert_eq!(
        plan.group_for(loaded_root)
            .expect("loaded group")
            .protection_reasons,
        vec![ArchiveExceptProtectionReason::Loaded]
    );
    assert_eq!(
        plan.group_for(open_root)
            .expect("open group")
            .protection_reasons,
        vec![ArchiveExceptProtectionReason::EdgeNotClosed]
    );
    Ok(())
}

#[tokio::test]
async fn orphan_missing_parent_ambiguous_relation_and_cycle_fail_closed_by_group() -> Result<()> {
    let keep = thread_id(30);
    let orphan = thread_id(31);
    let missing = thread_id(32);
    let missing_parent = thread_id(33);
    let ambiguous_parent_a = thread_id(34);
    let ambiguous_parent_b = thread_id(35);
    let ambiguous_child = thread_id(36);
    let cycle_a = thread_id(37);
    let cycle_b = thread_id(38);
    let runtime = runtime_with_threads(&[
        keep,
        orphan,
        missing,
        ambiguous_parent_a,
        ambiguous_parent_b,
        ambiguous_child,
        cycle_a,
        cycle_b,
    ])
    .await?;
    sqlx::query("UPDATE threads SET source = ? WHERE id = ?")
        .bind(r#"{"subAgent":{"threadSpawn":{}}}"#)
        .bind(orphan.to_string())
        .execute(runtime.pool.as_ref())
        .await?;
    sqlx::query("DROP TABLE thread_spawn_edges")
        .execute(runtime.pool.as_ref())
        .await?;
    sqlx::query("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT NOT NULL, child_thread_id TEXT NOT NULL, status TEXT NOT NULL)")
        .execute(runtime.pool.as_ref())
        .await?;
    for (parent, child) in [
        (missing_parent, missing),
        (ambiguous_parent_a, ambiguous_child),
        (ambiguous_parent_b, ambiguous_child),
        (cycle_a, cycle_b),
        (cycle_b, cycle_a),
    ] {
        sqlx::query("INSERT INTO thread_spawn_edges VALUES (?, ?, 'closed')")
            .bind(parent.to_string())
            .bind(child.to_string())
            .execute(runtime.pool.as_ref())
            .await?;
    }

    let plan = runtime.plan_archive_except(request(keep, &[])).await?;

    assert_eq!(
        plan.group_for(orphan)
            .expect("orphan group")
            .protection_reasons,
        vec![ArchiveExceptProtectionReason::OrphanSubagent]
    );
    assert_eq!(
        plan.group_for(missing)
            .expect("missing-parent group")
            .protection_reasons,
        vec![ArchiveExceptProtectionReason::MissingParent]
    );
    assert_eq!(
        plan.group_for(ambiguous_parent_a)
            .expect("ambiguous group")
            .protection_reasons,
        vec![ArchiveExceptProtectionReason::AmbiguousRelation]
    );
    assert_eq!(
        plan.group_for(cycle_a)
            .expect("cycle group")
            .protection_reasons,
        vec![ArchiveExceptProtectionReason::Cycle]
    );
    Ok(())
}

#[tokio::test]
async fn limit_skips_large_group_and_selects_later_smaller_group_without_splitting() -> Result<()> {
    let keep = thread_id(40);
    let large = thread_id(41);
    let large_a = thread_id(42);
    let large_b = thread_id(43);
    let small = thread_id(44);
    let runtime = runtime_with_threads(&[keep, large, large_a, large_b, small]).await?;
    for child in [large_a, large_b] {
        edge(
            &runtime,
            large,
            child,
            DirectionalThreadSpawnEdgeStatus::Closed,
        )
        .await?;
    }
    let limited = request(keep, &[]).with_limit(NonZeroUsize::new(2).expect("nonzero"));

    let plan = runtime.plan_archive_except(limited).await?;

    assert_eq!(
        plan.group_for(large).expect("large group").disposition,
        ArchiveExceptGroupDisposition::LimitSkipped
    );
    assert_eq!(
        plan.group_for(small).expect("small group").disposition,
        ArchiveExceptGroupDisposition::Candidate
    );
    assert_eq!(plan.candidate_thread_ids, vec![small]);
    assert_eq!(
        plan.subtrees,
        vec![ArchiveExceptSubtree {
            root_thread_id: small,
            thread_ids: vec![small],
        }]
    );
    Ok(())
}

#[tokio::test]
async fn archived_root_yields_one_archive_call_per_active_subtree() -> Result<()> {
    let keep = thread_id(50);
    let root = thread_id(51);
    let archived_middle = thread_id(52);
    let left = thread_id(53);
    let left_child = thread_id(54);
    let right = thread_id(55);
    let runtime =
        runtime_with_threads(&[keep, root, archived_middle, left, left_child, right]).await?;
    for (parent, child) in [
        (root, archived_middle),
        (archived_middle, left),
        (left, left_child),
        (root, right),
    ] {
        edge(
            &runtime,
            parent,
            child,
            DirectionalThreadSpawnEdgeStatus::Closed,
        )
        .await?;
    }
    mark_archived(&runtime, root).await?;
    mark_archived(&runtime, archived_middle).await?;

    let plan = runtime.plan_archive_except(request(keep, &[])).await?;
    let group = plan.group_for(root).expect("archived-root group");

    assert_eq!(
        group.subtrees,
        vec![
            ArchiveExceptSubtree {
                root_thread_id: left,
                thread_ids: vec![left, left_child],
            },
            ArchiveExceptSubtree {
                root_thread_id: right,
                thread_ids: vec![right],
            },
        ]
    );
    assert_eq!(group.disposition, ArchiveExceptGroupDisposition::Candidate);
    Ok(())
}

#[tokio::test]
async fn plan_rejects_missing_or_archived_keep_and_counts_archive_progress() -> Result<()> {
    let keep = thread_id(60);
    let missing = thread_id(61);
    let archived_keep = thread_id(62);
    let candidate = thread_id(63);
    let runtime = runtime_with_threads(&[keep, archived_keep, candidate]).await?;
    mark_archived(&runtime, archived_keep).await?;

    assert_eq!(
        runtime
            .plan_archive_except(request(missing, &[]))
            .await
            .expect_err("missing keep should fail")
            .to_string(),
        format!("archive-except keep thread {missing} was not found")
    );
    assert_eq!(
        runtime
            .plan_archive_except(request(archived_keep, &[]))
            .await
            .expect_err("archived keep should fail")
            .to_string(),
        format!("archive-except keep thread {archived_keep} is already archived")
    );
    assert_eq!(
        runtime
            .count_archived_threads(&[archived_keep, candidate])
            .await?,
        1
    );
    Ok(())
}

async fn mark_archived(runtime: &StateRuntime, thread_id: ThreadId) -> Result<()> {
    let metadata = runtime.get_thread(thread_id).await?.expect("thread exists");
    runtime
        .mark_archived(thread_id, &metadata.rollout_path, Utc::now())
        .await
}

#[test]
fn default_request_is_unlimited_and_preserves_loaded_ids() {
    let keep = thread_id(70);
    let loaded = thread_id(71);

    assert_eq!(
        ArchiveExceptRequest::new(keep, HashSet::from([loaded])),
        ArchiveExceptRequest {
            keep_thread_id: keep,
            loaded_thread_ids: HashSet::from([loaded]),
            limit: ArchiveExceptLimit::Unlimited,
        }
    );
}

#[test]
fn plan_group_lookup_returns_none_for_unknown_root() {
    let keep = thread_id(80);
    let plan = ArchiveExceptPlan {
        keep_thread_id: keep,
        protected_thread_ids: vec![],
        candidate_thread_ids: vec![],
        subtrees: vec![],
        groups: vec![],
    };

    assert_eq!(plan.group_for(keep), None);
}
