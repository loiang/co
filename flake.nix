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
          repo = pkgs.callPackage ./nix/repo.nix {
            inherit version;
            source = nixpkgs.lib.fileset.toSource {
              root = ./codex-rs/repo;
              fileset = ./codex-rs/repo/src;
            };
            backendSource = ./scripts/co;
            rustPlatform = rustPlatformFor pkgs;
          };
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
          ruff =
            assert pkgs.lib.versionAtLeast pkgs.ruff.version "0.15.8";
            pkgs.ruff;
          bazel = pkgs.writeShellApplication {
            name = "bazel";
            runtimeInputs = [ pkgs.findutils ];
            text =
              builtins.replaceStrings
                [
                  "@bazelisk@"
                  "@patchelf@"
                  "@loader@"
                  "@rpath@"
                ]
                [
                  "${pkgs.bazelisk}/bin/bazelisk"
                  "${pkgs.patchelf}/bin/patchelf"
                  "${pkgs.glibc}/lib/ld-linux-x86-64.so.2"
                  "${pkgs.lib.makeLibraryPath [
                    pkgs.glibc
                    pkgs.stdenv.cc.cc.lib
                  ]}"
                ]
                (builtins.readFile ./nix/bazel_nix_wrapper.sh);
          };
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
              pkgs.bazelisk
              bazel
              pkgs.cmake
              pkgs.dotslash
              pkgs.file
              pkgs.git
              pkgs.llvmPackages.clang
              pkgs.llvmPackages.libclang.lib
              pkgs.nix-prefetch-git
              pkgs.openssl
              pkgs.pkg-config
              ruff
              pkgs.uv
              python
            ];
            PKG_CONFIG_PATH = "${pkgs.openssl.dev}/lib/pkgconfig";
            LIBCLANG_PATH = "${pkgs.llvmPackages.libclang.lib}/lib";
            shellHook = ''
              export CO_NIX_DEV_ACTIVE=1
              export CO_NIX_RUFF_BIN=${ruff}/bin/ruff
              export BAZELISK_HOME="''${BAZELISK_HOME:-''${XDG_CACHE_HOME:-$HOME/.cache}/co-bazelisk}"
              export BAZEL_OUTPUT_USER_ROOT="''${BAZEL_OUTPUT_USER_ROOT:-''${XDG_CACHE_HOME:-$HOME/.cache}/co-bazel}"
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
