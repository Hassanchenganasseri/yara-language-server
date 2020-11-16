#!/usr/bin/env python3
import argparse
import asyncio
import logging
import logging.handlers
from os import environ
from pathlib import Path

from yarals.server import YaraLanguageServer


def _build_cli():
    parser = argparse.ArgumentParser(description="Start the YARA language server")
    parser.add_argument("host", help="Interface to bind server to")
    parser.add_argument("port", type=int, help="Port to bind server to")
    return parser.parse_args()

def _build_logger():
    ''' Configure the loggers appropriately '''
    log_file = Path(environ.get("HOME")).joinpath(".yara.log")
    # rename all the levels to align with the language client's logging format
    for lvl in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        logging.addLevelName(getattr(logging, lvl), lvl.capitalize())
    yara_logger = logging.getLogger("yara")
    screen_hdlr = logging.StreamHandler()
    screen_fmt = logging.Formatter("[%(levelname)-5s - %(asctime)s] %(name)s.%(module)s : %(message)s", datefmt="%-H:%M:%S %p")
    screen_hdlr.setFormatter(screen_fmt)
    screen_hdlr.setLevel(logging.INFO)
    file_hdlr = logging.handlers.RotatingFileHandler(filename=log_file, backupCount=1, maxBytes=100000)
    file_hdlr.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s | %(message)s"))
    file_hdlr.setLevel(logging.DEBUG)
    yara_logger.addHandler(screen_hdlr)
    yara_logger.addHandler(file_hdlr)
    yara_logger.setLevel(logging.DEBUG)
    return yara_logger

async def main():
    ''' Program entrypoint '''
    args = _build_cli()
    yarals = YaraLanguageServer()
    logger.info("Starting YARA IO language server")
    socket_server = await asyncio.start_server(
        client_connected_cb=yarals.handle_client,
        host=args.host,
        port=args.port,
        start_serving=False
    )
    servhost, servport = socket_server.sockets[0].getsockname()
    logger.info("Serving on tcp://%s:%d", servhost, servport)
    try:
        async with socket_server:
            await socket_server.serve_forever()
    except asyncio.CancelledError:
        logger.info("Server has successfully shutdown")

if __name__ == "__main__":
    try:
        logger = _build_logger()
        asyncio.run(main(), debug=True)
    except KeyboardInterrupt:
        logger.info("Ending per user request")
