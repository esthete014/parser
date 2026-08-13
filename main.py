import logging
import sys
import webbrowser

import uvicorn

from app.main import app

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 8080


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    url = f"http://{HOST}:{PORT}"
    logger.info("Открываю %s", url)
    webbrowser.open(url)

    logger.info("Сервер запущен на %s", url)

    try:
        uvicorn.run(
            "app.main:app",
            host=HOST,
            port=PORT,
            reload=False,
            log_level="info",
            timeout_graceful_shutdown=5,
        )
    except KeyboardInterrupt:
        logger.info("Прерывание...")
        sys.exit(0)


if __name__ == "__main__":
    main()
