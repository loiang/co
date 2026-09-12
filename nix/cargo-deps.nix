{
  runCommand,
  rustPlatform,
  lockFile,
  outputHashes,
}:

let
  rawCargoDeps = rustPlatform.importCargoLock {
    inherit lockFile outputHashes;
    extraRegistries = {
      "https://github.com/rust-lang/crates.io-index" = "https://static.crates.io/crates";
    };
  };
in
runCommand "cargo-vendor-dir" { } ''
  mkdir "$out"
  cp -a ${rawCargoDeps}/. "$out/"
  chmod u+w "$out/.cargo" "$out/.cargo/config.toml"
  sed -i '/^\[source\."https:\/\/github\.com\/rust-lang\/crates\.io-index"\]$/,+2d' \
    "$out/.cargo/config.toml"
  if grep -Fq '[source."https://github.com/rust-lang/crates.io-index"]' \
    "$out/.cargo/config.toml"; then
    echo "duplicate crates.io source remains in cargo vendor config" >&2
    exit 1
  fi
''
