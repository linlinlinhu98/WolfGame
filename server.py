# -*- coding: utf-8 -*-
"""Convenience entry point for the web server."""
import sys

# LLM speech may contain unicode the GBK console can't encode — replace
# instead of crashing the server (same guard as main.py).
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")

if __name__ == "__main__":
    from web_ui.server import app
    print("狼人杀 Web 平台")
    print("http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
