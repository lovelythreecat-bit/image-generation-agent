"""Bounded in-process workers; interrupted generation is never automatically replayed."""

import asyncio


class Worker:
    def __init__(self, task_service, concurrency=1):
        self.task_service = task_service
        self.concurrency = concurrency
        self.tasks = []
        self.stopping = asyncio.Event()
        self.pending_interruptions = set()

    async def start(self):
        if self.tasks:
            return
        await self.task_service.repository.interrupt_running()
        self.stopping.clear()
        self.tasks = [asyncio.create_task(self._loop()) for _ in range(self.concurrency)]

    async def stop(self):
        self.stopping.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def run_once(self):
        # Retry only metadata writes after outages, never the provider operation.
        for identifier in tuple(self.pending_interruptions):
            await self.task_service.repository.finish(
                identifier,
                status="interrupted",
                error_code="database_unavailable",
                error_message="Execution metadata was not saved; explicit recovery is required",
            )
            self.pending_interruptions.discard(identifier)
        task = await self.task_service.repository.claim()
        if task is None:
            return False
        try:
            await self.task_service.execute(task)
        except Exception:
            self.pending_interruptions.add(task.id)
            raise
        return True

    async def _loop(self):
        while not self.stopping.is_set():
            try:
                found = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # DB outage leaves the claimed row running for explicit recovery.
                # No raw database or provider exception reaches logs.
                found = False
            if not found:
                try:
                    await asyncio.wait_for(self.stopping.wait(), timeout=0.25)
                except TimeoutError:
                    pass
