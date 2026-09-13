use codex_protocol::ThreadId;
use std::collections::HashSet;
use std::num::NonZeroUsize;

/// Maximum number of active threads selected by one archive plan.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum ArchiveExceptLimit {
    #[default]
    Unlimited,
    MaxThreads(NonZeroUsize),
}

/// Inputs that define one fail-closed archive planning snapshot.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ArchiveExceptRequest {
    pub keep_thread_id: ThreadId,
    pub loaded_thread_ids: HashSet<ThreadId>,
    pub limit: ArchiveExceptLimit,
}

impl ArchiveExceptRequest {
    pub fn new(keep_thread_id: ThreadId, loaded_thread_ids: HashSet<ThreadId>) -> Self {
        Self {
            keep_thread_id,
            loaded_thread_ids,
            limit: ArchiveExceptLimit::Unlimited,
        }
    }

    pub fn with_limit(mut self, limit: NonZeroUsize) -> Self {
        self.limit = ArchiveExceptLimit::MaxThreads(limit);
        self
    }
}

/// Stable safety reasons that protect an entire connected session group.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ArchiveExceptProtectionReason {
    Loaded,
    Pinned,
    EdgeNotClosed,
    OrphanSubagent,
    MissingParent,
    AmbiguousRelation,
    Cycle,
}

/// The planner's decision for one complete connected session group.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ArchiveExceptGroupDisposition {
    Protected,
    AlreadyArchived,
    Candidate,
    LimitSkipped,
}

/// One native archive root and the active descendants it is expected to archive.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ArchiveExceptSubtree {
    pub root_thread_id: ThreadId,
    pub thread_ids: Vec<ThreadId>,
}

/// One root session and every descendant, treated as an atomic selection group.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ArchiveExceptGroup {
    pub root_thread_id: ThreadId,
    pub member_thread_ids: Vec<ThreadId>,
    pub target_thread_ids: Vec<ThreadId>,
    pub subtrees: Vec<ArchiveExceptSubtree>,
    pub protection_reasons: Vec<ArchiveExceptProtectionReason>,
    pub created_at_ms: i64,
    pub disposition: ArchiveExceptGroupDisposition,
}

/// Deterministically ordered complete-group decisions from one State DB snapshot.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ArchiveExceptPlan {
    pub keep_thread_id: ThreadId,
    pub protected_thread_ids: Vec<ThreadId>,
    pub candidate_thread_ids: Vec<ThreadId>,
    pub subtrees: Vec<ArchiveExceptSubtree>,
    pub groups: Vec<ArchiveExceptGroup>,
}

impl ArchiveExceptPlan {
    pub fn group_for(&self, root_thread_id: ThreadId) -> Option<&ArchiveExceptGroup> {
        self.groups
            .iter()
            .find(|group| group.root_thread_id == root_thread_id)
    }
}
