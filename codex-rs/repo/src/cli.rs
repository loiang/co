use clap::Arg;
use clap::ArgAction;
use clap::Command;

pub(crate) fn command() -> Command {
    Command::new("repo")
        .about("Repository lifecycle commands")
        .subcommand_required(true)
        .disable_help_subcommand(true)
        .subcommand(
            Command::new("upgrade")
                .arg(Arg::new("revision").default_value("upstream/main"))
                .arg(ni_repo())
                .arg(cores()),
        )
        .subcommand(
            Command::new("upgrade-finalize")
                .arg(Arg::new("upstream-rev").long("upstream-rev").required(true))
                .arg(ni_repo())
                .arg(cores()),
        )
        .subcommand(Command::new("promote").arg(Arg::new("candidate").required(true)))
        .subcommand(Command::new("test"))
        .subcommand(Command::new("build").arg(cores()))
        .subcommand(Command::new("test-host").arg(ni_repo()))
        .subcommand(Command::new("publish").arg(dry_run()))
        .subcommand(
            Command::new("install")
                .arg(Arg::new("host").required(true))
                .arg(Arg::new("tag"))
                .arg(ni_repo())
                .arg(dry_run()),
        )
}

fn ni_repo() -> Arg {
    Arg::new("ni-repo")
        .long("ni-repo")
        .value_parser(clap::value_parser!(std::path::PathBuf))
        .default_value("/repo/ni")
}

fn cores() -> Arg {
    Arg::new("cores")
        .long("cores")
        .value_parser(clap::value_parser!(u64))
        .default_value("0")
}

fn dry_run() -> Arg {
    Arg::new("dry-run")
        .long("dry-run")
        .action(ArgAction::SetTrue)
}
