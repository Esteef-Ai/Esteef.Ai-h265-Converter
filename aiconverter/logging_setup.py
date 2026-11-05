import logging
import logging.handlers
import os

from .config import CONFIG


def setup_logging():
    os.makedirs(CONFIG["log_dir"], exist_ok=True)
    log_path = os.path.join(CONFIG["log_dir"], "aiconverter.log")

    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8"
    )
    console = logging.StreamHandler()
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    console.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(console)

    # werkzeug/waitress access noise is not useful for a headless service log
    logging.getLogger("waitress").setLevel(logging.WARNING)
