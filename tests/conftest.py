"""Point backburner at a throwaway data dir BEFORE the package is imported,
so tests never touch the real ~/.backburner database."""

import os
import tempfile

os.environ["BACKBURNER_HOME"] = tempfile.mkdtemp(prefix="backburner-test-")
