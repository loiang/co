use super::LoadedThreadCollector;
use codex_app_server_protocol::ThreadLoadedListResponse;
use codex_protocol::ThreadId;
use pretty_assertions::assert_eq;

fn page(data: &[ThreadId], next_cursor: Option<&str>) -> ThreadLoadedListResponse {
    ThreadLoadedListResponse {
        data: data.iter().map(ToString::to_string).collect(),
        next_cursor: next_cursor.map(ToString::to_string),
    }
}

#[test]
fn accumulates_every_loaded_page() {
    let first = ThreadId::from_u128(1);
    let second = ThreadId::from_u128(2);
    let mut collector = LoadedThreadCollector::default();

    assert_eq!(
        collector.accept_page(page(&[first], Some("next"))),
        Ok(Some("next".to_string()))
    );
    assert_eq!(collector.accept_page(page(&[second], None)), Ok(None));
    assert_eq!(collector.thread_ids, [first, second].into_iter().collect());
}

#[test]
fn rejects_repeated_cursor_and_invalid_thread_id() {
    let mut collector = LoadedThreadCollector::default();
    assert_eq!(
        collector.accept_page(page(&[], Some("same"))),
        Ok(Some("same".to_string()))
    );
    assert_eq!(
        collector.accept_page(page(&[], Some("same"))),
        Err("the app server repeated a loaded-thread pagination cursor".to_string())
    );
    assert_eq!(
        LoadedThreadCollector::default().accept_page(ThreadLoadedListResponse {
            data: vec!["not-a-thread-id".to_string()],
            next_cursor: None,
        }),
        Err("the app server returned an invalid loaded thread id".to_string())
    );
}
