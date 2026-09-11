"""Entry point: `python run.py` starts the app and opens your browser."""
import threading
import webbrowser

import uvicorn

HOST = "127.0.0.1"
PORT = 8787


def _open_browser():
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    threading.Timer(1.5, _open_browser).start()
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)
