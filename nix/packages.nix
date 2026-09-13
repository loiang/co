{
  pkgs,
  codexSource,
  version,
  rustToolchainVersion,
}:

let
  staticPkgs = pkgs.pkgsStatic;
  rust = pkgs.rust-bin.stable.${rustToolchainVersion}.minimal.override {
    targets = [ "x86_64-unknown-linux-musl" ];
  };
  rustPlatform = staticPkgs.makeRustPlatform {
    cargo = rust;
    rustc = rust;
  };
  codexCli = import ./codex-cli.nix {
    inherit (pkgs)
      binutils
      clang
      cmake
      fetchurl
      file
      gitMinimal
      lib
      libclang
      perl
      pkg-config
      runCommand
      ;
    inherit
      rustPlatform
      version
      ;
    source = codexSource;
    stdenv = staticPkgs.stdenv;
  };
in
{
  codex = codexCli;
  codex-cli = codexCli;
  codex-cargo-deps = codexCli.cargoDeps;
  default = codexCli;
}
