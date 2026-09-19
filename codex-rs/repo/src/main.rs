mod backend;
mod cli;

fn main() {
    let args: Vec<_> = std::env::args_os().collect();
    cli::command().get_matches_from(&args);
    match backend::run(&args[1..]) {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("repo: {error:#}");
            std::process::exit(1);
        }
    }
}
