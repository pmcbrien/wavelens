# PlatformIO pre-script: define WL_VERSION from companion/VERSION so the firmware reports the same
# version number as the Pi bridge built from the same commit. (Runs via exec: no __file__; use the project dir.)
import os
Import("env")  # noqa: F821  (PlatformIO injects it)
here = env.subst("$PROJECT_DIR")  # noqa: F821
try:
    v = open(os.path.join(here, "..", "..", "VERSION")).read().strip() or "dev"
except OSError:
    v = "dev"
env.Append(CPPDEFINES=[("WL_VERSION", env.StringifyMacro(v))])  # noqa: F821
