use crate::app_server_session::AppServerSession;
use codex_app_server_protocol::ThreadLoadedListParams;
use codex_app_server_protocol::ThreadLoadedListResponse;
use codex_protocol::ThreadId;
use std::collections::HashSet;

pub(super) async fn load_all_loaded_thread_ids(
    app_server: &mut AppServerSession,
) -> Result<HashSet<ThreadId>, String> {
    let mut loaded = LoadedThreadCollector::default();
    let mut cursor = None;
    loop {
        let page = app_server
            .thread_loaded_list(ThreadLoadedListParams {
                cursor,
                limit: None,
            })
            .await
            .map_err(|error| format!("failed to query loaded sessions safely: {error}"))?;
        cursor = loaded.accept_page(page)?;
        if cursor.is_none() {
            return Ok(loaded.thread_ids);
        }
    }
}

#[derive(Default)]
struct LoadedThreadCollector {
    thread_ids: HashSet<ThreadId>,
    seen_cursors: HashSet<String>,
}

impl LoadedThreadCollector {
    fn accept_page(&mut self, page: ThreadLoadedListResponse) -> Result<Option<String>, String> {
        for raw_thread_id in page.data {
            let thread_id = ThreadId::from_string(&raw_thread_id)
                .map_err(|_| "the app server returned an invalid loaded thread id".to_string())?;
            self.thread_ids.insert(thread_id);
        }
        if let Some(cursor) = &page.next_cursor
            && !self.seen_cursors.insert(cursor.clone())
        {
            return Err("the app server repeated a loaded-thread pagination cursor".to_string());
        }
        Ok(page.next_cursor)
    }
}

#[cfg(test)]
#[path = "archive_except_loaded_tests.rs"]
mod tests;
