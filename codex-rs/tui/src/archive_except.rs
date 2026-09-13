use std::num::NonZeroUsize;

pub(crate) const ARCHIVE_EXCEPT_USAGE: &str =
    "Usage: /archive-except [--preview|--dry-run] [--limit N] (N > 0)";

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(crate) enum ArchiveExceptMode {
    #[default]
    Execute,
    Preview,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(crate) struct ArchiveExceptOptions {
    pub(crate) mode: ArchiveExceptMode,
    pub(crate) limit: Option<NonZeroUsize>,
}

impl ArchiveExceptOptions {
    pub(crate) fn parse(args: &str) -> Result<Self, &'static str> {
        let mut options = Self::default();
        let mut tokens = args.split_whitespace();
        while let Some(token) = tokens.next() {
            match token {
                "--preview" | "--dry-run" => options.mode = ArchiveExceptMode::Preview,
                "--limit" => {
                    let value = tokens.next().ok_or(ARCHIVE_EXCEPT_USAGE)?;
                    options.limit = Some(parse_limit(value)?);
                }
                _ => {
                    let Some(value) = token.strip_prefix("--limit=") else {
                        return Err(ARCHIVE_EXCEPT_USAGE);
                    };
                    options.limit = Some(parse_limit(value)?);
                }
            }
        }
        Ok(options)
    }
}

fn parse_limit(value: &str) -> Result<NonZeroUsize, &'static str> {
    value
        .parse::<usize>()
        .ok()
        .and_then(NonZeroUsize::new)
        .ok_or(ARCHIVE_EXCEPT_USAGE)
}

#[cfg(test)]
#[path = "archive_except_tests.rs"]
mod tests;
