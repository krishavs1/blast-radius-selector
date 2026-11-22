"""Entry point: imports the selector as a module so its compiled form is reused."""
import sys

sys.path.insert(0, "/app")
import selector  # noqa: E402

selector.main()
