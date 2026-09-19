{
  lib,
  rustPlatform,
  source,
  backendSource,
  version,
  runCommand,
  makeWrapper,
  python3,
  gitMinimal,
}:

rustPlatform.buildRustPackage {
  pname = "repo";
  inherit version;
  src = source;
  sourceRoot = "co-codex-rs-source";
  cargoDeps = import ./cargo-deps.nix {
    inherit runCommand rustPlatform;
    lockFile = ../codex-rs/Cargo.lock;
    outputHashes = import ./cargo-git-hashes.nix;
  };
  cargoBuildFlags = [
    "--package"
    "codex-repo"
    "--bin"
    "repo"
  ];
  nativeBuildInputs = [ makeWrapper ];
  doCheck = false;

  postInstall = ''
    mkdir -p "$out/libexec/repo"
    cp ${backendSource}/*.py "$out/libexec/repo/"
    wrapProgram "$out/bin/repo" \
      --set REPO_BACKEND_PATH "$out/libexec/repo/cli.py" \
      --set REPO_PYTHON "${lib.getExe python3}" \
      --prefix PATH : "${lib.makeBinPath [ gitMinimal ]}"
  '';

  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    "$out/bin/repo" --help
    ${lib.getExe python3} "$out/libexec/repo/cli.py" --help
    runHook postInstallCheck
  '';

  meta = {
    description = "Repository governance CLI with its packaged Python backend";
    homepage = "https://github.com/loiang/co";
    license = lib.licenses.asl20;
    mainProgram = "repo";
    platforms = lib.platforms.unix;
  };
}
