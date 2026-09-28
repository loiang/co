/// Version reported by release packages.
///
/// Source builds retain the workspace version, while the package builder supplies
/// the release version as a compile-time environment variable.
pub(crate) const CLI_VERSION: &str = match option_env!("CODEX_CLI_VERSION") {
    Some(version) => version,
    None => env!("CARGO_PKG_VERSION"),
};
