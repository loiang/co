{
  lib,
  stdenvNoCC,
  makeWrapper,
  python3,
  codexCli,
  source,
  version,
}:

let
  python = python3.withPackages (pythonPackages: [ pythonPackages.websockets ]);
in
stdenvNoCC.mkDerivation {
  pname = "codex";
  inherit version;
  src = source;
  sourceRoot = "co-runtime-helpers";
  nativeBuildInputs = [
    makeWrapper
    python
  ];

  installPhase = ''
    runHook preInstall
    install -Dm755 "${codexCli}/bin/codex" "$out/bin/codex"
    install -Dm755 codex-archive-subagents "$out/bin/codex-archive-subagents"
    mkdir -p "$out/libexec/codex-archive-subagents"
    cp -R codex_sessions "$out/libexec/codex-archive-subagents/codex_sessions"
    patchShebangs "$out/bin/codex-archive-subagents"
    wrapProgram "$out/bin/codex-archive-subagents" \
      --prefix PYTHONPATH : "$out/libexec/codex-archive-subagents"
    runHook postInstall
  '';

  meta = codexCli.meta // {
    description = "OpenAI Codex CLI source build with runtime helpers";
  };
}
