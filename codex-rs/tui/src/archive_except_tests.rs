use super::ARCHIVE_EXCEPT_USAGE;
use super::ArchiveExceptMode;
use super::ArchiveExceptOptions;
use pretty_assertions::assert_eq;
use std::num::NonZeroUsize;

#[test]
fn parses_preview_alias_and_positive_limit() {
    assert_eq!(
        ArchiveExceptOptions::parse("--dry-run --limit=7"),
        Ok(ArchiveExceptOptions {
            mode: ArchiveExceptMode::Preview,
            limit: NonZeroUsize::new(7),
        })
    );
}

#[test]
fn rejects_zero_missing_invalid_and_unknown_arguments() {
    for args in ["--limit 0", "--limit", "--limit nope", "--unknown"] {
        assert_eq!(
            ArchiveExceptOptions::parse(args),
            Err(ARCHIVE_EXCEPT_USAGE),
            "args: {args}"
        );
    }
}
