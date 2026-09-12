{
  cmake,
  llvmPackages,
  openssl,
  libcap ? null,
  rustPlatform,
  pkg-config,
  runCommand,
  lib,
  stdenv,
  version ? "0.0.0",
  ...
}:
let
  cargoDeps = import ../nix/cargo-deps.nix {
    inherit runCommand rustPlatform;
    lockFile = ./Cargo.lock;
    outputHashes = import ../nix/cargo-git-hashes.nix;
  };
in
rustPlatform.buildRustPackage (_: {
  env.PKG_CONFIG_PATH = lib.makeSearchPathOutput "dev" "lib/pkgconfig" (
    [ openssl ] ++ lib.optionals stdenv.isLinux [ libcap ]
  );
  pname = "codex-rs";
  inherit version;
  inherit cargoDeps;
  doCheck = false;
  src = ./.;

  # Patch the workspace Cargo.toml so that cargo embeds the correct version in
  # CARGO_PKG_VERSION (which the binary reads via env!("CARGO_PKG_VERSION")).
  # On release commits the Cargo.toml already contains the real version and
  # this sed is a no-op.
  postPatch = ''
    sed -i 's/^version = "0\.0\.0"$/version = "${version}"/' Cargo.toml
  '';
  nativeBuildInputs = [
    cmake
    llvmPackages.clang
    llvmPackages.libclang.lib
    openssl
    pkg-config
  ]
  ++ lib.optionals stdenv.isLinux [
    libcap
  ];

  meta = with lib; {
    description = "OpenAI Codex command‑line interface rust implementation";
    license = licenses.asl20;
    homepage = "https://github.com/openai/codex";
    mainProgram = "codex";
  };
})
