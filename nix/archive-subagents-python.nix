{ pkgs }:

let
  python = pkgs.python3.withPackages (pythonPackages: [ pythonPackages.websockets ]);
in
pkgs.writeShellApplication {
  name = "codex-archive-subagents-python";
  text = ''
    exec ${python}/bin/python "$@"
  '';
}
