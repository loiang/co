{
  lib,
  rustPlatform,
  source,
  backendSource,
  version,
  makeWrapper,
  python3,
  gitMinimal,
}:

rustPlatform.buildRustPackage {
  pname = "co";
  inherit version;
  src = source;
  cargoDeps = rustPlatform.importCargoLock {
    lockFile = ./repo-cargo/Cargo.lock;
  };
  postPatch = ''
    cp ${./repo-cargo/Cargo.toml} Cargo.toml
    cp ${./repo-cargo/Cargo.lock} Cargo.lock
  '';
  cargoBuildFlags = [
    "--package"
    "codex-repo"
    "--bin"
    "co"
  ];
  nativeBuildInputs = [ makeWrapper ];
  doCheck = false;

  postInstall = ''
    mkdir -p "$out/libexec/repo"
    cp ${backendSource}/*.py "$out/libexec/repo/"
    wrapProgram "$out/bin/co" \
      --set REPO_BACKEND_PATH "$out/libexec/repo/cli.py" \
      --set REPO_PYTHON "${lib.getExe python3}" \
      --prefix PATH : "${lib.makeBinPath [ gitMinimal ]}"
  '';

  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    "$out/bin/co" --help
    ${lib.getExe python3} "$out/libexec/repo/cli.py" --help
    runHook postInstallCheck
  '';

  meta = {
    description = "Repository governance CLI with its packaged Python backend";
    homepage = "https://github.com/loiang/co";
    license = lib.licenses.asl20;
    mainProgram = "co";
    platforms = lib.platforms.unix;
  };
}
