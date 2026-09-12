{
  lib,
  stdenv,
  rustPlatform,
  source,
  version,
  fetchurl,
  clang,
  cmake,
  gitMinimal,
  libclang,
  perl,
  pkg-config,
  runCommand,
  binutils,
  file,
}:

let
  cargoDeps = import ./cargo-deps.nix {
    inherit runCommand rustPlatform;
    lockFile = ../codex-rs/Cargo.lock;
    outputHashes = import ./cargo-git-hashes.nix;
  };
in
rustPlatform.buildRustPackage {
  pname = "codex-cli";
  inherit version;

  src = source;
  sourceRoot = "co-codex-rs-source";
  inherit cargoDeps;

  __structuredAttrs = true;
  cargoBuildFlags = [
    "--package"
    "codex-cli"
    "--bin"
    "codex"
  ];
  cargoCheckFlags = [
    "--package"
    "codex-cli"
  ];

  nativeBuildInputs = [
    clang
    cmake
    gitMinimal
    libclang
    perl
    pkg-config
  ];

  env = {
    AWS_LC_SYS_NO_JITTER_ENTROPY = "1";
    AWS_LC_SYS_NO_JITTER_ENTROPY_x86_64_unknown_linux_musl = "1";
    LIBCLANG_PATH = "${lib.getLib libclang}/lib";
    RUSTY_V8_ARCHIVE = fetchurl {
      url = "https://github.com/openai/codex/releases/download/rusty-v8-v150.4.0/librusty_v8_ptrcomp_sandbox_release_x86_64-unknown-linux-musl.a.gz";
      hash = "sha256-0G4IvL9FqQz+rIpNMixyiHdcteNgnKcD6jErFVF05Go=";
    };
    RUSTY_V8_SRC_BINDING_PATH = fetchurl {
      url = "https://github.com/openai/codex/releases/download/rusty-v8-v150.4.0/src_binding_ptrcomp_sandbox_release_x86_64-unknown-linux-musl.rs";
      hash = "sha256-dyeCauR5vbZF6Acjn7EtH44uI956bPFvXuWSaQ0dhQY=";
    };
    NIX_CFLAGS_COMPILE = toString (
      lib.optionals stdenv.cc.isGNU [ "-Wno-error=stringop-overflow" ]
      ++ lib.optionals stdenv.cc.isClang [ "-Wno-error=character-conversion" ]
    );
  };

  preBuild = ''
    export NIX_CFLAGS_LINK=
  '';

  doCheck = false;
  doInstallCheck = true;
  nativeInstallCheckInputs = [
    binutils
    file
  ];
  installCheckPhase = ''
    runHook preInstallCheck
    file "$out/bin/codex"
    file "$out/bin/codex" | grep -E 'static(-pie|ally) linked'
    ! readelf -l "$out/bin/codex" | grep -F 'Requesting program interpreter'
    ! readelf -d "$out/bin/codex" | grep -E '\((NEEDED|RPATH|RUNPATH)\)'
    "$out/bin/codex" --version
    runHook postInstallCheck
  '';

  meta = {
    description = "OpenAI Codex CLI built statically from this source tree";
    homepage = "https://github.com/loiang/co";
    license = lib.licenses.asl20;
    mainProgram = "codex";
    platforms = [ "x86_64-linux" ];
  };
}
