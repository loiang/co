//! Observe the entry points' first RPC without requiring an interactive terminal.

use super::*;
use crate::app_server_session::ThreadParamsMode;
use crate::legacy_core::config::ConfigBuilder;
use crate::legacy_core::config::ConfigOverrides;
use crate::local_settings::LocalSettings;
use codex_app_server_protocol::JSONRPCMessage;
use futures::SinkExt;
use pretty_assertions::assert_eq;
use serde_json::json;
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::Message;

async fn initial_list_params(
    action: SessionPickerAction,
    mode: ThreadParamsMode,
) -> Result<(ThreadListParams, ThreadListParams)> {
    let home = tempfile::tempdir()?;
    let config = ConfigBuilder::default()
        .codex_home(home.path().to_path_buf())
        .harness_overrides(ConfigOverrides {
            cwd: Some(home.path().to_path_buf()),
            ..ConfigOverrides::default()
        })
        .build()
        .await?;
    let settings = LocalSettings::from(&config);
    let remote = mode == ThreadParamsMode::Remote;
    let cwd = config.cwd.to_path_buf();
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let endpoint = crate::resolve_remote_addr(&format!("ws://{}", listener.local_addr()?))?;
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await?;
        let mut socket = tokio_tungstenite::accept_async(stream).await?;
        while let Some(message) = socket.next().await {
            let Message::Text(text) = message? else {
                continue;
            };
            let JSONRPCMessage::Request(request) = serde_json::from_str(&text)? else {
                continue;
            };
            let result = match request.method.as_str() {
                "initialize" => json!({"userAgent": "picker-source-policy-test/1.0"}),
                "thread/list" => {
                    return Ok::<_, color_eyre::Report>(
                        serde_json::from_value::<ThreadListParams>(
                            request.params.expect("thread/list params"),
                        )?,
                    );
                }
                method => panic!("unexpected picker request: {method}"),
            };
            socket
                .send(Message::Text(
                    json!({"id": request.id, "result": result})
                        .to_string()
                        .into(),
                ))
                .await?;
        }
        color_eyre::eyre::bail!("connection closed before the initial thread/list")
    });
    let session = AppServerSession::new(crate::connect_remote_app_server(endpoint).await?, mode)
        .with_remote_cwd_override(Some(cwd.clone()));
    let mut tui = crate::tui::test_support::make_test_tui()?;
    tui.set_alt_screen_enabled(/*enabled*/ false);
    // Both entry points enqueue their first load before yielding. Only poll that
    // prefix: drawing or acquiring terminal input may fail on a headless runner,
    // and the interactive loop itself is outside this RPC contract.
    match action {
        SessionPickerAction::Resume => {
            let handle = session.request_handle();
            let mut picker = Box::pin(run_resume_picker_with_launch_context(
                remote,
                &mut tui,
                &config,
                &settings,
                /*show_all*/ false,
                /*include_non_interactive*/ false,
                session,
                handle,
                SessionPickerLaunchContext::Startup,
            ));
            let _ = futures::poll!(picker.as_mut());
        }
        SessionPickerAction::Fork => {
            let mut picker = Box::pin(run_fork_picker_with_app_server(
                remote, &mut tui, &config, &settings, /*show_all*/ false, session,
            ));
            let _ = futures::poll!(picker.as_mut());
        }
    }
    let actual =
        tokio::time::timeout(std::time::Duration::from_secs(/*secs*/ 10), server).await???;
    let expected = thread_list_params(
        /*cursor*/ None,
        Some(ThreadListCwdFilter::One(cwd.to_string_lossy().into_owned())),
        SessionStatus::Active,
        if remote {
            ProviderFilter::Any
        } else {
            ProviderFilter::MatchDefault(config.model_provider_id.clone())
        },
        ThreadSortKey::UpdatedAt,
        /*include_non_interactive*/ false,
        /*use_state_db_only*/ true,
    );
    Ok((actual, expected))
}

#[tokio::test]
async fn resume_entry_starts_from_state_db_for_local_and_remote_workspaces() -> Result<()> {
    for mode in [ThreadParamsMode::Embedded, ThreadParamsMode::Remote] {
        let (actual, expected) = initial_list_params(SessionPickerAction::Resume, mode).await?;
        assert_eq!(actual, expected, "resume workspace: {mode:?}");
    }
    Ok(())
}

#[tokio::test]
async fn fork_entry_preserves_local_state_db_and_remote_store_default() -> Result<()> {
    for mode in [ThreadParamsMode::Embedded, ThreadParamsMode::Remote] {
        let (actual, mut expected) = initial_list_params(SessionPickerAction::Fork, mode).await?;
        expected.use_state_db_only = mode == ThreadParamsMode::Embedded;
        assert_eq!(actual, expected, "fork workspace: {mode:?}");
    }
    Ok(())
}
