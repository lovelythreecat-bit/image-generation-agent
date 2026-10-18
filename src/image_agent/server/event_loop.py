"""Use buffered-protocol-compatible sockets for asyncmy on Windows."""
import asyncio


def selector_loop_factory():
    # Uvicorn 0.36+ owns loop creation and ignores the legacy policy here.
    # asyncmy's buffered protocol cannot resize its buffer on Proactor reads.
    return asyncio.SelectorEventLoop()
