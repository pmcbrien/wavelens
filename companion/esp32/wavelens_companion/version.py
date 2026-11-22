# PlatformIO pre-script: define WL_VERSION from companion/VERSION so the firmware reports the same
# version number as the Pi bridge built from the same commit. (Runs via exec: no __file__; use the project dir.)
#
# WL_BUILD is the commit the image was built from (7 hex digits, "local" when git isn't there). Fixes to one
# module build ship without a VERSION bump so the other chips aren't offered an update, and then two images
# with different code print the same version: the boot line also prints the build, so a serial log shows
# which one is on the board.
import os
import subprocess
Import("env")  # noqa: F821  (PlatformIO injects it)
here = env.subst("$PROJECT_DIR")  # noqa: F821
try:
    v = open(os.path.join(here, "..", "..", "VERSION")).read().strip() or "dev"
except OSError:
    v = "dev"
b = (os.environ.get("GITHUB_SHA") or "")[:7]
if not b:
    try:
        b = subprocess.check_output(["git", "rev-parse", "--short=7", "HEAD"], cwd=here,
                                    stderr=subprocess.DEVNULL).decode().strip()
    except (OSError, subprocess.CalledProcessError):
        b = ""
env.Append(CPPDEFINES=[("WL_VERSION", env.StringifyMacro(v)),  # noqa: F821
                       ("WL_BUILD", env.StringifyMacro(b or "local"))])  # noqa: F821
