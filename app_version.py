"""
app_version.py -- the small version number shown at the bottom left of every screen (Jay, 2026-10-11: "v2.xxxx where you increment the
xxxx for every deploy and I will tell you when we increment the major version number").

  v2.0269
   |  `---- the BUILD: the number of the Cloud Run revision that is answering. Cloud Run makes a new revision on every deploy, so the
   |        number goes up by itself with each deploy: nothing to remember, nothing to edit, and it always tells the truth about
   |        what is running. Each service counts on its own: the dev site and the production site have different build numbers.
   `------- the MAJOR version: APP_MAJOR below. Change it ONLY when Jay says to start a new major version.

Run on a laptop (no revision) it reads v2.dev.
"""
from __future__ import annotations

import os
import re

APP_MAJOR = 2

_REVISION = re.compile(r"-(\d{3,6})-[a-z0-9]+$")


def build_number(revision: str | None = None) -> int | None:
    """The build number from a Cloud Run revision name such as checkreq-website-dev-00269-t8d (269), or None."""
    rev = os.environ.get("K_REVISION", "") if revision is None else revision
    m = _REVISION.search(rev or "")
    return int(m.group(1)) if m else None


def label(revision: str | None = None) -> str:
    """The text shown on the screen: v2.0269, or v2.dev when there is no revision (a local run)."""
    n = build_number(revision)
    return f"v{APP_MAJOR}.{n:04d}" if n is not None else f"v{APP_MAJOR}.dev"
