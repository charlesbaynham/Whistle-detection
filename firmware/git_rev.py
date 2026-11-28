# PlatformIO pre-script: bake the short commit hash (plus "+" if the tree is dirty) into GIT_REV.
# $GIT_REV overrides it (scripts/flash.sh builds on a flasher from a tree without .git).
import os
import subprocess

Import("env")  # noqa: F821 (SCons global)

rev = os.environ.get("GIT_REV")
if not rev:
    try:
        run = lambda *a: subprocess.run(["git", *a], cwd=env["PROJECT_DIR"], capture_output=True, text=True)  # noqa: F821
        rev = run("rev-parse", "--short=7", "HEAD").stdout.strip() or "dev"
        if run("status", "--porcelain", "--", ".").stdout.strip():
            rev += "+"
    except OSError:
        rev = "dev"
env.Append(CPPDEFINES=[("GIT_REV", env.StringifyMacro(rev))])  # noqa: F821
