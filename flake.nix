{
  description = "Development and source-build Nix flake for OpenAI Codex CLI";

  inputs = {
    build-version = {
      url = "path:./nix/build-version";
      flake = false;
    };
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    rust-overlay = {
      url = "github:oxalica/rust-overlay";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      build-version,
      self,
      nixpkgs,
      rust-overlay,
      ...
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      rawVersion = nixpkgs.lib.removeSuffix "\n" (builtins.readFile (build-version + "/version"));
      version =
        if builtins.match "(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)" rawVersion != null then
          rawVersion
        else
          throw "build-version must contain a stable SemVer";
      rustToolchainToml = builtins.fromTOML (builtins.readFile ./codex-rs/rust-toolchain.toml);
      rustToolchainVersion = rustToolchainToml.toolchain.channel;
      codexSource = nixpkgs.lib.cleanSourceWith {
        src = self.outPath + "/codex-rs";
        filter =
          path: type:
          let
            name = builtins.baseNameOf path;
          in
          nixpkgs.lib.cleanSourceFilter path type
          && !builtins.elem name [
            "target"
          ];
        name = "co-codex-rs-source";
      };
      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          overlays = [ rust-overlay.overlays.default ];
        };
      rustMinimalFor = pkgs: pkgs.rust-bin.stable.${rustToolchainVersion}.minimal;
      rustPlatformFor =
        pkgs:
        pkgs.makeRustPlatform {
          cargo = rustMinimalFor pkgs;
          rustc = rustMinimalFor pkgs;
        };
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          upstreamCodexRs = pkgs.callPackage ./codex-rs {
            inherit version;
            rustPlatform = rustPlatformFor pkgs;
          };
          customPackages = nixpkgs.lib.optionalAttrs (system == "x86_64-linux") (
            import ./nix/packages.nix {
              inherit
                pkgs
                codexSource
                rustToolchainVersion
                version
                ;
            }
          );
        in
        {
          codex-rs = upstreamCodexRs;
          default = upstreamCodexRs;
        }
        // customPackages
      );

      checks = forAllSystems (
        system:
        nixpkgs.lib.optionalAttrs (system == "x86_64-linux") (
          let
            pkgs = pkgsFor system;
            package = self.packages.${system}.codex;
          in
          {
            codex-release-layout = pkgs.runCommand "codex-release-layout" { } ''
              test -x ${package}/bin/codex
              test ! -e ${package}/bin/codex-archive-subagents
              test ! -e ${package}/bin/codex-code-mode-host
              touch "$out"
            '';
          }
        )
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          rust = pkgs.rust-bin.stable.${rustToolchainVersion}.default.override {
            extensions = [
              "rust-analyzer"
              "rust-src"
            ];
          };
          python = pkgs.python3.withPackages (pythonPackages: [
            pythonPackages.pytest
            pythonPackages.websockets
          ]);
        in
        {
          default = pkgs.mkShell {
            name = "co-development-${system}";
            packages = [
              rust
              pkgs.cargo-nextest
              pkgs.cmake
              pkgs.file
              pkgs.just
              pkgs.llvmPackages.clang
              pkgs.llvmPackages.libclang.lib
              pkgs.nix-prefetch-git
              pkgs.openssl
              pkgs.pkg-config
              pkgs.ruff
              python
            ];
            PKG_CONFIG_PATH = "${pkgs.openssl.dev}/lib/pkgconfig";
            LIBCLANG_PATH = "${pkgs.llvmPackages.libclang.lib}/lib";
            shellHook = ''
              export CO_NIX_DEV_ACTIVE=1
              export CARGO_HOME="''${XDG_CACHE_HOME:-$HOME/.cache}/co-cargo"
              export CC=clang
              export CXX=clang++
            '';
          };
        }
      );

      formatter = forAllSystems (system: (pkgsFor system).nixfmt);
    };
}
