use super::*;
use crate::app_server_session::ThreadParamsMode;
use codex_state::ArchiveExceptPlan;
use pretty_assertions::assert_eq;
use serde_json::json;
use tokio::net::TcpListener;

async fn seed_archive_except_threads(app: &mut App) -> Result<Vec<ThreadId>> {
    let runtime = codex_state::StateRuntime::init(
        app.config.sqlite.clone(),
        app.config.model_provider_id.clone(),
    )
    .await
    .map_err(std::io::Error::other)?;
    runtime
        .mark_backfill_complete(/*last_watermark*/ None)
        .await
        .map_err(std::io::Error::other)?;
    let mut ids = Vec::new();
    for label in ["keep", "first", "second"] {
        let filename_ts = "2025-02-01T10-00-00";
        let timestamp = "2025-02-01T10:00:00Z";
        let id = app_test_support::create_fake_rollout(
            &app.config.codex_home,
            filename_ts,
            timestamp,
            label,
            Some(&app.config.model_provider_id),
            /*git_info*/ None,
        )
        .map_err(std::io::Error::other)?;
        let path = app_test_support::rollout_path(&app.config.codex_home, filename_ts, &id);
        let id = ThreadId::from_string(&id)?;
        let metadata = codex_state::ThreadMetadataBuilder::new(
            id,
            path,
            chrono::DateTime::parse_from_rfc3339(timestamp)?.with_timezone(&chrono::Utc),
            serde_json::from_value(json!("cli"))?,
        )
        .build(&app.config.model_provider_id);
        runtime
            .upsert_thread(&metadata)
            .await
            .map_err(std::io::Error::other)?;
        ids.push(id);
    }
    app.active_thread_id = Some(ids[0]);
    app.state_db = Some(runtime);
    Ok(ids)
}

async fn confirm_archive_except_popup(
    app: &mut App,
    session: &mut AppServerSession,
    events: &mut tokio::sync::mpsc::UnboundedReceiver<AppEvent>,
) -> Result<ArchiveExceptPlan> {
    let mut tui = crate::tui::test_support::make_test_tui()?;
    app.handle_event(
        &mut tui,
        session,
        AppEvent::PrepareArchiveExcept(crate::archive_except::ArchiveExceptOptions::default()),
    )
    .await?;
    app.chat_widget
        .handle_key_event(KeyEvent::from(KeyCode::Down));
    app.chat_widget
        .handle_key_event(KeyEvent::from(KeyCode::Enter));
    while let Ok(event) = events.try_recv() {
        if let AppEvent::ConfirmArchiveExcept(plan) = event {
            return Ok(plan);
        }
    }
    color_eyre::eyre::bail!("confirmation popup did not emit its archive plan")
}

#[tokio::test]
async fn archive_except_lifecycle_embedded_archives_confirmed_threads() -> Result<()> {
    let (mut app, mut events, _ops) = make_test_app_with_channels().await;
    let ids = seed_archive_except_threads(&mut app).await?;
    let runtime = app.state_db.clone().expect("fixture database");
    let mut session = crate::start_embedded_app_server_for_picker(&app.config).await?;
    let plan = confirm_archive_except_popup(&mut app, &mut session, &mut events).await?;
    assert_eq!(plan.candidate_thread_ids.len(), 2);
    assert_eq!(
        runtime
            .count_archived_threads(&ids)
            .await
            .map_err(std::io::Error::other)?,
        0
    );
    let mut tui = crate::tui::test_support::make_test_tui()?;
    app.handle_event(
        &mut tui,
        &mut session,
        AppEvent::ConfirmArchiveExcept(plan.clone()),
    )
    .await?;
    assert_eq!(
        runtime
            .count_archived_threads(&plan.candidate_thread_ids)
            .await
            .map_err(std::io::Error::other)?,
        2
    );
    assert_eq!(
        runtime
            .count_archived_threads(&[ids[0]])
            .await
            .map_err(std::io::Error::other)?,
        0
    );
    for id in &plan.candidate_thread_ids {
        let metadata = runtime
            .get_thread(*id)
            .await
            .map_err(std::io::Error::other)?
            .expect("archived thread");
        assert!(metadata.archived_at.is_some());
        assert!(metadata.rollout_path.is_file());
        assert!(
            metadata
                .rollout_path
                .starts_with(app.config.codex_home.join("archived_sessions"))
        );
    }
    session.shutdown().await?;
    Ok(())
}

#[derive(Clone, Copy)]
enum ArchiveFault {
    IncompleteArchive,
    NextSubtreeBecomesLoaded,
}

#[tokio::test]
async fn archive_except_lifecycle_stops_after_incomplete_archive_or_changed_replan() -> Result<()> {
    for fault in [
        ArchiveFault::IncompleteArchive,
        ArchiveFault::NextSubtreeBecomesLoaded,
    ] {
        let (mut app, mut events, _ops) = make_test_app_with_channels().await;
        let ids = seed_archive_except_threads(&mut app).await?;
        let runtime = app.state_db.clone().expect("fixture database");
        let server_runtime = runtime.clone();
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let endpoint = crate::resolve_remote_addr(&format!("ws://{}", listener.local_addr()?))?;
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await?;
            let mut archived_root = None;
            super::disconnect::serve_reconnect_requests(
                tokio_tungstenite::accept_async(stream).await?,
                move |request| {
                    let runtime = server_runtime.clone();
                    let response = match request.method.as_str() {
                        "thread/loaded/list" => {
                            let loaded = if archived_root.is_some() {
                                ids[1..].iter().map(ToString::to_string).collect::<Vec<_>>()
                            } else {
                                Vec::new()
                            };
                            Some(json!({"result": {"data": loaded, "nextCursor": null}}))
                        }
                        "thread/archive" => {
                            assert!(archived_root.is_none(), "must stop before the next archive");
                            archived_root = Some(
                                ThreadId::from_string(
                                    request.params.as_ref().unwrap()["threadId"]
                                        .as_str()
                                        .unwrap(),
                                )
                                .unwrap(),
                            );
                            Some(json!({"result": {}}))
                        }
                        method => panic!("unexpected request: {method}"),
                    };
                    let mark = if request.method == "thread/archive"
                        && matches!(fault, ArchiveFault::NextSubtreeBecomesLoaded)
                    {
                        archived_root
                    } else {
                        None
                    };
                    async move {
                        if let Some(id) = mark {
                            let metadata = runtime.get_thread(id).await.unwrap().unwrap();
                            runtime
                                .mark_archived(id, &metadata.rollout_path, chrono::Utc::now())
                                .await
                                .unwrap();
                        }
                        response
                    }
                },
            )
            .await
        });
        // Keep App's embedded eligibility gate, substituting only the transport to inject faults.
        let mut session = AppServerSession::new(
            crate::connect_remote_app_server(endpoint).await?,
            ThreadParamsMode::Remote,
        );
        let plan = confirm_archive_except_popup(&mut app, &mut session, &mut events).await?;
        assert_eq!(plan.candidate_thread_ids.len(), 2);
        let mut tui = crate::tui::test_support::make_test_tui()?;
        app.handle_event(
            &mut tui,
            &mut session,
            AppEvent::ConfirmArchiveExcept(plan.clone()),
        )
        .await?;
        let (archived, loaded_requests, reason) = match fault {
            ArchiveFault::IncompleteArchive => (0, 2, "did not archive the complete subtree"),
            ArchiveFault::NextSubtreeBecomesLoaded => (1, 3, "became unsafe during execution"),
        };
        assert_eq!(
            runtime
                .count_archived_threads(&plan.candidate_thread_ids)
                .await
                .map_err(std::io::Error::other)?,
            archived
        );
        let mut history = String::new();
        while let Ok(event) = events.try_recv() {
            if let AppEvent::InsertHistoryCell(cell) = event {
                history.push_str(
                    &cell
                        .display_lines(200)
                        .iter()
                        .map(ToString::to_string)
                        .collect::<Vec<_>>()
                        .join("\n"),
                );
            }
        }
        assert!(
            history.contains(&format!("{archived} of 2 confirmed sessions")),
            "{history}"
        );
        assert!(history.contains(reason), "{history}");
        session.shutdown().await?;
        let methods = server.await??;
        assert_eq!(
            methods
                .iter()
                .filter(|method| *method == "thread/archive")
                .count(),
            1
        );
        assert_eq!(
            methods
                .iter()
                .filter(|method| *method == "thread/loaded/list")
                .count(),
            loaded_requests
        );
    }
    Ok(())
}
