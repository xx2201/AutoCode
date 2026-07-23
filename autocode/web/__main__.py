"""Run the AutoCode web interface with Uvicorn."""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    host = os.getenv("AUTOCODE_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("AUTOCODE_WEB_PORT", "8765"))
    ssl_keyfile = os.getenv("AUTOCODE_WEB_SSL_KEYFILE") or None
    ssl_certfile = os.getenv("AUTOCODE_WEB_SSL_CERTFILE") or None
    uvicorn.run(
        "autocode.web.app:create_app",
        factory=True,
        host=host,
        port=port,
        ssl_keyfile=ssl_keyfile,
        ssl_certfile=ssl_certfile,
        proxy_headers=False,
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":
    main()
