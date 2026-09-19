mod backend;
mod backend_metadata;
mod cli;
mod sync;

fn main() {
    let args: Vec<_> = std::env::args_os().collect();
    let matches = cli::command().get_matches_from(&args);
    if matches.subcommand_name() == Some("sync") {
        match sync::execute() {
            Ok(code) => std::process::exit(code),
            Err(error) => {
                eprintln!("co sync: {error:#}");
                std::process::exit(1);
            }
        }
    }
    match backend::run(&args[1..]) {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("co: {error:#}");
            std::process::exit(1);
        }
    }
}
