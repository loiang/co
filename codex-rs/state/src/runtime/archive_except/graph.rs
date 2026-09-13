use codex_protocol::ThreadId;
use std::cmp::Ordering;
use std::collections::HashMap;
use std::collections::HashSet;

pub(super) struct ThreadRecord {
    pub(super) id: ThreadId,
    pub(super) source: String,
    pub(super) archived: bool,
    pub(super) pinned: bool,
    pub(super) created_at_ms: i64,
}

impl TryFrom<(String, String, bool, bool, i64)> for ThreadRecord {
    type Error = anyhow::Error;

    fn try_from(row: (String, String, bool, bool, i64)) -> Result<Self, Self::Error> {
        Ok(Self {
            id: ThreadId::try_from(row.0)?,
            source: row.1,
            archived: row.2,
            pinned: row.3,
            created_at_ms: row.4,
        })
    }
}

pub(super) struct EdgeRecord {
    parent: ThreadId,
    child: ThreadId,
    status: String,
}

impl TryFrom<(String, String, String)> for EdgeRecord {
    type Error = anyhow::Error;

    fn try_from(row: (String, String, String)) -> Result<Self, Self::Error> {
        Ok(Self {
            parent: ThreadId::try_from(row.0)?,
            child: ThreadId::try_from(row.1)?,
            status: row.2,
        })
    }
}

pub(super) struct ArchiveExceptGraph {
    pub(super) records: HashMap<ThreadId, ThreadRecord>,
    pub(super) parent_by_child: HashMap<ThreadId, ThreadId>,
    pub(super) children_by_parent: HashMap<ThreadId, HashSet<ThreadId>>,
    adjacency: HashMap<ThreadId, HashSet<ThreadId>>,
    pub(super) missing_parent: HashSet<ThreadId>,
    pub(super) ambiguous: HashSet<ThreadId>,
    pub(super) edge_not_closed: HashSet<ThreadId>,
}

impl ArchiveExceptGraph {
    pub(super) fn from_rows(
        thread_rows: Vec<ThreadRecord>,
        edge_rows: Vec<EdgeRecord>,
    ) -> anyhow::Result<Self> {
        let mut records = HashMap::new();
        let mut ambiguous = HashSet::new();
        for record in thread_rows {
            let id = record.id;
            if records.insert(id, record).is_some() {
                ambiguous.insert(id);
            }
        }
        let mut relations = HashMap::<ThreadId, Vec<EdgeRecord>>::new();
        for edge in edge_rows {
            if records.contains_key(&edge.child) {
                relations.entry(edge.child).or_default().push(edge);
            }
        }
        let mut graph = Self {
            adjacency: records.keys().map(|id| (*id, HashSet::new())).collect(),
            records,
            parent_by_child: HashMap::new(),
            children_by_parent: HashMap::new(),
            missing_parent: HashSet::new(),
            ambiguous,
            edge_not_closed: HashSet::new(),
        };
        graph.add_relations(relations);
        Ok(graph)
    }

    pub(super) fn components(&self) -> Vec<HashSet<ThreadId>> {
        let mut remaining = self.records.keys().copied().collect::<HashSet<_>>();
        let mut components = Vec::new();
        while let Some(start) = remaining.iter().min_by(|a, b| thread_id_cmp(a, b)).copied() {
            let mut pending = vec![start];
            let mut component = HashSet::new();
            while let Some(thread_id) = pending.pop() {
                if component.insert(thread_id) {
                    pending.extend(self.adjacency[&thread_id].iter().copied());
                }
            }
            remaining.retain(|id| !component.contains(id));
            components.push(component);
        }
        components
    }

    fn add_relations(&mut self, relations: HashMap<ThreadId, Vec<EdgeRecord>>) {
        for (child, child_relations) in relations {
            if child_relations.len() > 1 {
                self.ambiguous.insert(child);
            }
            if !self.records[&child].archived
                && child_relations.iter().any(|edge| edge.status != "closed")
            {
                self.edge_not_closed.insert(child);
            }
            let parents = child_relations
                .iter()
                .map(|edge| edge.parent)
                .collect::<HashSet<_>>();
            if parents.len() == 1 {
                self.parent_by_child
                    .insert(child, *parents.iter().next().expect("one parent"));
            }
            for parent in parents {
                if !self.records.contains_key(&parent) {
                    self.missing_parent.insert(child);
                    continue;
                }
                self.adjacency.entry(child).or_default().insert(parent);
                self.adjacency.entry(parent).or_default().insert(child);
                self.children_by_parent
                    .entry(parent)
                    .or_default()
                    .insert(child);
            }
        }
    }
}

pub(super) fn thread_id_cmp(left: &ThreadId, right: &ThreadId) -> Ordering {
    left.to_string().cmp(&right.to_string())
}
