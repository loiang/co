use super::ArchiveExceptPlan;
use super::ArchiveExceptSubtree;
use super::StateRuntime;
use crate::DirectionalThreadSpawnEdgeStatus;
use crate::runtime::test_support::test_thread_metadata;
use crate::runtime::test_support::unique_temp_dir;
use anyhow::Result;
use chrono::Utc;
use codex_protocol::ThreadId;
use codex_utils_absolute_path::test_support::PathExt;
use pretty_assertions::assert_eq;
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
    for &id in thread_ids {
        runtime
            .upsert_thread(&test_thread_metadata(&codex_home, id, codex_home.clone()))
            .await?;
    }
    Ok(runtime)
}

async fn edge(runtime: &StateRuntime, parent: ThreadId, child: ThreadId) -> Result<()> {
    runtime
        .upsert_thread_spawn_edge(parent, child, DirectionalThreadSpawnEdgeStatus::Open)
        .await
}

#[tokio::test]
async fn plan_protects_keep_ancestors_and_descendants_but_archives_sibling_subtrees() -> Result<()>
{
    let root = thread_id(1);
    let keep = thread_id(2);
    let keep_child = thread_id(3);
    let sibling = thread_id(4);
    let sibling_child = thread_id(5);
    let other_root = thread_id(6);
    let runtime =
        runtime_with_threads(&[root, keep, keep_child, sibling, sibling_child, other_root]).await?;
    edge(&runtime, root, keep).await?;
    edge(&runtime, keep, keep_child).await?;
    edge(&runtime, root, sibling).await?;
    edge(&runtime, sibling, sibling_child).await?;

    assert_eq!(
        runtime.plan_archive_except(keep).await?,
        ArchiveExceptPlan {
            keep_thread_id: keep,
            protected_thread_ids: vec![root, keep, keep_child],
            candidate_thread_ids: vec![sibling, sibling_child, other_root],
            subtrees: vec![
                ArchiveExceptSubtree {
                    root_thread_id: sibling,
                    thread_ids: vec![sibling, sibling_child],
                },
                ArchiveExceptSubtree {
                    root_thread_id: other_root,
                    thread_ids: vec![other_root],
                },
            ],
        }
    );
    Ok(())
}

#[tokio::test]
async fn plan_without_edges_archives_every_other_active_thread_as_a_root() -> Result<()> {
    let keep = thread_id(10);
    let first = thread_id(11);
    let second = thread_id(12);
    let runtime = runtime_with_threads(&[keep, first, second]).await?;

    let plan = runtime.plan_archive_except(keep).await?;

    assert_eq!(plan.protected_thread_ids, vec![keep]);
    assert_eq!(plan.candidate_thread_ids, vec![first, second]);
    assert_eq!(
        plan.subtrees,
        vec![
            ArchiveExceptSubtree {
                root_thread_id: first,
                thread_ids: vec![first],
            },
            ArchiveExceptSubtree {
                root_thread_id: second,
                thread_ids: vec![second],
            },
        ]
    );
    Ok(())
}

#[tokio::test]
async fn plan_rejects_missing_or_archived_keep_thread() -> Result<()> {
    let active = thread_id(20);
    let missing = thread_id(21);
    let archived = thread_id(22);
    let runtime = runtime_with_threads(&[active, archived]).await?;
    let archived_metadata = runtime
        .get_thread(archived)
        .await?
        .expect("archived thread");
    runtime
        .mark_archived(archived, &archived_metadata.rollout_path, Utc::now())
        .await?;

    assert_eq!(
        runtime
            .plan_archive_except(missing)
            .await
            .expect_err("missing keep should fail")
            .to_string(),
        format!("archive-except keep thread {missing} was not found")
    );
    assert_eq!(
        runtime
            .plan_archive_except(archived)
            .await
            .expect_err("archived keep should fail")
            .to_string(),
        format!("archive-except keep thread {archived} is already archived")
    );
    Ok(())
}

#[tokio::test]
async fn plan_excludes_archived_threads_and_counts_actual_archive_progress() -> Result<()> {
    let keep = thread_id(30);
    let archived = thread_id(31);
    let active = thread_id(32);
    let runtime = runtime_with_threads(&[keep, archived, active]).await?;
    let archived_metadata = runtime
        .get_thread(archived)
        .await?
        .expect("archived thread");
    runtime
        .mark_archived(archived, &archived_metadata.rollout_path, Utc::now())
        .await?;

    let plan = runtime.plan_archive_except(keep).await?;

    assert_eq!(plan.candidate_thread_ids, vec![active]);
    assert_eq!(
        runtime.count_archived_threads(&[archived, active]).await?,
        1
    );
    Ok(())
}

#[tokio::test]
async fn plan_fails_closed_when_spawn_edges_contain_a_cycle() -> Result<()> {
    let keep = thread_id(40);
    let first = thread_id(41);
    let second = thread_id(42);
    let runtime = runtime_with_threads(&[keep, first, second]).await?;
    edge(&runtime, first, second).await?;
    edge(&runtime, second, first).await?;

    let error = runtime
        .plan_archive_except(keep)
        .await
        .expect_err("cyclic graph should fail closed");

    assert_eq!(
        error.to_string(),
        "archive-except cannot safely plan a cyclic thread graph"
    );
    Ok(())
}
