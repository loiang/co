use super::*;
use crate::app_server_session::ThreadParamsMode;
use codex_app_server_client::RemoteAppServerEndpoint;
use codex_protocol::protocol::SessionSource;
use codex_state::ArchiveExceptGroupDisposition;
use codex_state::ArchiveExceptPlan;
use codex_state::ArchiveExceptRequest;
use codex_state::DirectionalThreadSpawnEdgeStatus;
use codex_state::StateRuntime;
use codex_utils_absolute_path::AbsolutePathBuf;
use color_eyre::eyre::ensure;
use color_eyre::eyre::eyre;

#[derive(PartialEq, Eq)]
struct ArchiveState {
    archived: bool,
    path: PathBuf,
}

struct LiveSnapshot {
    threads: HashMap<ThreadId, ArchiveState>,
    edges: HashMap<(ThreadId, ThreadId), DirectionalThreadSpawnEdgeStatus>,
}

async fn live_snapshot(runtime: &StateRuntime, plan: &ArchiveExceptPlan) -> Result<LiveSnapshot> {
    let mut threads = HashMap::new();
    let mut parents = HashSet::new();
    for id in plan
        .groups
        .iter()
        .flat_map(|group| &group.member_thread_ids)
    {
        let Some(metadata) = runtime
            .get_thread(*id)
            .await
            .map_err(std::io::Error::other)?
        else {
            continue;
        };
        parents.insert(*id);
        if let Ok(source) = serde_json::from_str::<SessionSource>(&metadata.source)
            && let Some(parent) = source.parent_thread_id()
        {
            parents.insert(parent);
        }
        threads.insert(
            *id,
            ArchiveState {
                archived: metadata.archived_at.is_some(),
                path: metadata.rollout_path,
            },
        );
    }
    let mut edges = HashMap::new();
    for parent in parents {
        for status in [
            DirectionalThreadSpawnEdgeStatus::Open,
            DirectionalThreadSpawnEdgeStatus::Closed,
        ] {
            for child in runtime
                .list_thread_spawn_children_with_status(parent, status)
                .await
                .map_err(std::io::Error::other)?
            {
                edges.insert((parent, child), status);
            }
        }
    }
    Ok(LiveSnapshot { threads, edges })
}

fn live_env(name: &str) -> Result<String> {
    std::env::var(name).map_err(|_| eyre!("explicit opt-in requires {name}"))
}

/// Explicitly opt in to planning or archiving real groups on an existing daemon.
/// Ordinary runs ignore this test; archive modes require preflight scope confirmation.
#[tokio::test]
#[ignore = "requires explicit real home/socket/mode; archive modes mutate real sessions"]
async fn archive_except_live_one_real_group() -> Result<()> {
    let mode = live_env("CO_ARCHIVE_EXCEPT_LIVE_MODE")?;
    ensure!(
        matches!(
            mode.as_str(),
            "preflight" | "preflight-all" | "archive-one" | "archive-all"
        ),
        "invalid live mode"
    );
    let home =
        AbsolutePathBuf::from_absolute_path_checked(live_env("CO_ARCHIVE_EXCEPT_LIVE_HOME")?)?;
    let socket_path =
        AbsolutePathBuf::from_absolute_path_checked(live_env("CO_ARCHIVE_EXCEPT_LIVE_SOCKET")?)?;
    let sqlite = codex_state::SqliteConfig::from_sqlite_home(home.clone());
    color_eyre::eyre::ensure!(
        sqlite.state_db_path().is_file(),
        "real state database must already exist"
    );
    color_eyre::eyre::ensure!(
        socket_path.exists(),
        "local daemon socket must already exist"
    );
    let endpoint = RemoteAppServerEndpoint::UnixSocket { socket_path };
    let mut session = AppServerSession::new(
        crate::connect_remote_app_server(endpoint.clone()).await?,
        ThreadParamsMode::Remote,
    );
    color_eyre::eyre::ensure!(
        session.server_codex_home() == Some(home.to_string_lossy().as_ref()),
        "daemon home differs from explicit real home"
    );
    let loaded = crate::app::archive_except_loaded::load_all_loaded_thread_ids(&mut session)
        .await
        .map_err(color_eyre::eyre::Error::msg)?;
    let keep = loaded
        .iter()
        .min_by_key(|id| id.to_string())
        .copied()
        .ok_or_else(|| color_eyre::eyre::eyre!("live daemon has no loaded thread to keep"))?;
    let runtime = StateRuntime::init(sqlite.clone(), "openai".to_string())
        .await
        .map_err(std::io::Error::other)?;
    let (mut app, mut events, _ops) = make_test_app_with_channels().await;
    app.config.codex_home = home.clone();
    app.config.sqlite = sqlite;
    app.state_db = Some(runtime.clone());
    app.active_thread_id = Some(keep);
    app.app_server_target = crate::AppServerTarget::LocalDaemon {
        endpoint,
        allow_embedded_fallback: false,
    };
    app.prepare_archive_except(
        &mut session,
        crate::archive_except::ArchiveExceptOptions::default(),
    )
    .await;
    app.chat_widget
        .handle_key_event(KeyEvent::from(KeyCode::Down));
    app.chat_widget
        .handle_key_event(KeyEvent::from(KeyCode::Enter));
    let mut confirmed = None;
    while let Ok(event) = events.try_recv() {
        if let AppEvent::ConfirmArchiveExcept(plan) = event {
            confirmed = Some(plan);
        }
    }
    let mut plan = confirmed.ok_or_else(|| {
        color_eyre::eyre::eyre!("real production preparation did not produce an archive plan")
    })?;
    color_eyre::eyre::ensure!(
        loaded
            .iter()
            .all(|id| !plan.candidate_thread_ids.contains(id)),
        "loaded thread included in candidate plan"
    );
    let before = live_snapshot(&runtime, &plan).await?;
    let original_plan = plan.clone();
    let all = mode.ends_with("-all");
    let group = plan
        .groups
        .iter()
        .filter(|group| group.disposition == ArchiveExceptGroupDisposition::Candidate)
        .min_by_key(|group| {
            let open = before.edges.iter().any(|((_, child), status)| {
                *status == DirectionalThreadSpawnEdgeStatus::Open
                    && group.target_thread_ids.contains(child)
            });
            (!open, group.target_thread_ids.len())
        })
        .cloned()
        .ok_or_else(|| color_eyre::eyre::eyre!("no eligible real archive group"))?;
    color_eyre::eyre::ensure!(
        group.protection_reasons.is_empty(),
        "selected real group is protected"
    );
    if !all {
        plan.groups.retain(|item| {
            item.disposition != ArchiveExceptGroupDisposition::Candidate
                || item.root_thread_id == group.root_thread_id
        });
        plan.candidate_thread_ids = group.target_thread_ids.clone();
        plan.subtrees = group.subtrees.clone();
    }
    let selected = plan
        .candidate_thread_ids
        .iter()
        .copied()
        .collect::<HashSet<_>>();
    let selected_groups = plan
        .groups
        .iter()
        .filter(|item| item.disposition == ArchiveExceptGroupDisposition::Candidate)
        .count();
    color_eyre::eyre::ensure!(
        selected.iter().all(
            |id| before.threads.get(id).is_some_and(|state| !state.archived
                && state.path.is_file()
                && state.path.starts_with(home.as_path().join("sessions")))
        ),
        "candidate requires real existing active rollout files"
    );
    let open_edges = before
        .edges
        .iter()
        .filter(|((_, child), status)| {
            **status == DirectionalThreadSpawnEdgeStatus::Open && selected.contains(child)
        })
        .count();
    println!(
        "live preflight: loaded={} protected_groups={} candidate_groups={} selected_root={} selected_groups={} selected_threads={} incoming_open_edges={}",
        loaded.len(),
        original_plan
            .groups
            .iter()
            .filter(|group| group.disposition == ArchiveExceptGroupDisposition::Protected)
            .count(),
        plan.groups
            .iter()
            .filter(|group| group.disposition == ArchiveExceptGroupDisposition::Candidate)
            .count(),
        group.root_thread_id,
        selected_groups,
        selected.len(),
        open_edges
    );
    if mode.starts_with("preflight") {
        session.shutdown().await?;
        return Ok(());
    }
    if all {
        ensure!(
            live_env("CO_ARCHIVE_EXCEPT_LIVE_GROUPS")? == selected_groups.to_string(),
            "preflight group count changed; archive-all did not execute"
        );
    } else {
        ensure!(
            live_env("CO_ARCHIVE_EXCEPT_LIVE_ROOT")? == group.root_thread_id.to_string(),
            "preflight root changed; archive-one did not execute"
        );
    }
    app.confirm_archive_except(&mut session, plan.clone()).await;
    // Snapshot the original complete group membership, including all unselected and protected threads.
    let after = live_snapshot(&runtime, &original_plan).await?;
    let archived = selected
        .iter()
        .filter(|id| after.threads.get(id).is_some_and(|state| state.archived))
        .count();
    let closed = before
        .edges
        .iter()
        .filter(|(edge, status)| {
            **status == DirectionalThreadSpawnEdgeStatus::Open
                && selected.contains(&edge.1)
                && after.edges.get(edge) == Some(&DirectionalThreadSpawnEdgeStatus::Closed)
        })
        .count();
    let fresh_loaded = crate::app::archive_except_loaded::load_all_loaded_thread_ids(&mut session)
        .await
        .map_err(color_eyre::eyre::Error::msg)?;
    let fresh = runtime
        .plan_archive_except(ArchiveExceptRequest::new(keep, fresh_loaded))
        .await
        .map_err(std::io::Error::other)?;
    let remaining = fresh
        .groups
        .iter()
        .filter(|item| item.disposition == ArchiveExceptGroupDisposition::Candidate)
        .count();
    println!(
        "live archive observed: selected_groups={selected_groups} selected_threads={} archived_threads={archived} open_edges_closed={closed} remaining_candidate_groups={remaining}",
        selected.len()
    );
    for (id, state) in &before.threads {
        let current = after.threads.get(id).ok_or_else(|| {
            color_eyre::eyre::eyre!("thread disappeared during live verification")
        })?;
        if selected.contains(id) {
            color_eyre::eyre::ensure!(
                current.archived
                    && current.path.is_file()
                    && current
                        .path
                        .starts_with(home.as_path().join("archived_sessions")),
                "selected thread was not archived to an existing real rollout"
            );
        } else {
            color_eyre::eyre::ensure!(
                current == state,
                "an unselected or protected thread changed archive state"
            );
        }
    }
    for (edge, status) in &before.edges {
        let expected = if selected.contains(&edge.1) {
            DirectionalThreadSpawnEdgeStatus::Closed
        } else {
            *status
        };
        color_eyre::eyre::ensure!(
            after.edges.get(edge) == Some(&expected),
            "incoming edge did not match the selected archive scope"
        );
    }
    println!(
        "live archive verified: groups={selected_groups} threads={} open_edges_closed={} unselected_threads_unchanged={}",
        selected.len(),
        open_edges,
        before.threads.len() - selected.len()
    );
    session.shutdown().await?;
    Ok(())
}
