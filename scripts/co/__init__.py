"""Repository-local lifecycle support for the customized Codex CLI.

The package keeps upgrade, build, publication, and installation side effects
behind the root ``just`` interface so installed runtime packages cannot mutate
or publish a source checkout.
"""
