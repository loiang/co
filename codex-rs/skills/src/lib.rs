mod interface;
mod invocation;
mod loading;
mod mentions;
mod model;
mod name_counts;
mod parser;
mod selection;

pub use interface::SkillInterfaceAssetPolicy;
pub use interface::SkillInterfaceFile;
pub use interface::resolve_skill_interface;
pub use invocation::ImplicitSkillAccess;
pub use invocation::ImplicitSkillLookup;
pub use invocation::detect_implicit_skill_invocation_for_command;
pub use invocation::implicit_skill_accesses_for_command;
pub use loading::LoadedSkillRoot;
pub use loading::LoadedSkills;
pub use loading::SkillError;
pub use loading::SkillLoadFuture;
pub use loading::SkillRootLoadRequest;
pub use loading::SkillRootLoader;
pub use loading::SkillRootSnapshotCache;
pub use loading::SkillRootSnapshots;
pub use mentions::ToolMentionKind;
pub use mentions::ToolMentions;
pub use mentions::app_id_from_path;
pub use mentions::extract_tool_mentions;
pub use mentions::extract_tool_mentions_with_sigil;
pub use mentions::normalize_skill_path;
pub use mentions::plugin_config_name_from_path;
pub use mentions::tool_kind_for_path;
pub use model::EnvironmentSkillMetadata;
pub use model::SkillDependencies;
pub use model::SkillInterface;
pub use model::SkillMetadata;
pub use model::SkillPolicy;
pub use model::SkillToolDependency;
pub use name_counts::build_skill_name_counts;
pub use parser::ParsedSkillFrontmatter;
pub use parser::SkillParseError;
pub use parser::parse_skill_frontmatter_metadata;
pub use selection::ExplicitSkillLookup;
pub use selection::collect_explicit_skill_mentions;

use codex_utils_absolute_path::AbsolutePathBuf;
use std::fs;

use thiserror::Error;

const SYSTEM_SKILLS_DIR_NAME: &str = ".system";
const SKILLS_DIR_NAME: &str = "skills";

/// Returns the on-disk cache location for embedded system skills from an absolute CODEX_HOME.
pub fn system_cache_root_dir(codex_home: &AbsolutePathBuf) -> AbsolutePathBuf {
    codex_home
        .join(SKILLS_DIR_NAME)
        .join(SYSTEM_SKILLS_DIR_NAME)
}

/// Removes any bundled system skills from `CODEX_HOME/skills/.system`.
pub fn remove_system_skills(codex_home: &AbsolutePathBuf) -> Result<(), SystemSkillsError> {
    let dest_system = system_cache_root_dir(codex_home);
    if dest_system.as_path().exists() {
        fs::remove_dir_all(dest_system.as_path())
            .map_err(|source| SystemSkillsError::io("remove existing system skills dir", source))?;
    }
    Ok(())
}

#[derive(Debug, Error)]
pub enum SystemSkillsError {
    #[error("io error while {action}: {source}")]
    Io {
        action: &'static str,
        #[source]
        source: std::io::Error,
    },
}

impl SystemSkillsError {
    fn io(action: &'static str, source: std::io::Error) -> Self {
        Self::Io { action, source }
    }
}
