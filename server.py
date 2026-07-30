# -*- coding: utf-8 -*-
"""Convenience entry point for the web server."""
if __name__ == "__main__":
    from web_ui.server import app
    print("狼人杀 Web 平台")
    print("http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
