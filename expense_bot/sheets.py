import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import gspread

from .config import Settings
from .credentials import google_credentials
from .models import HEADERS


class SheetConflict(RuntimeError):
    """Reserved cells were modified; do not overwrite unrelated records."""


class Sheets:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="google-sheets")
        self.client = None
        self.ws = None

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self.executor, partial(fn, *args))

    async def connect(self):
        await self._run(self._connect)

    def _connect(self):
        credentials = google_credentials(self.settings)
        self.client = gspread.authorize(credentials)
        self.client.set_timeout(self.settings.google_timeout_seconds)
        book = self.client.open_by_key(self.settings.google_spreadsheet_id)
        try:
            self.ws = book.worksheet(self.settings.google_worksheet_name)
        except gspread.WorksheetNotFound:
            self.ws = book.add_worksheet(
                title=self.settings.google_worksheet_name, rows=1000, cols=5
            )
        values = self.ws.get("A:E", value_render_option="UNFORMATTED_VALUE")
        if not values:
            self.ws.update(range_name="A1:E1", values=[HEADERS], value_input_option="RAW")
            self.ws.freeze(rows=1)
            self.ws.format("A1:E1", {"textFormat": {"bold": True}})
            self.ws.format("C2:C", {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}})
        elif values[0] != HEADERS:
            raise SheetConflict("Заголовки A1:E1 не совпадают с требуемыми")

    async def rows(self):
        return await self._run(self._rows)

    def _rows(self):
        values = self.ws.get("A:E", value_render_option="UNFORMATTED_VALUE")
        if not values or values[0] != HEADERS:
            raise SheetConflict("Заголовки таблицы изменены")
        return list(values[1:])

    async def next_row(self) -> int:
        return len(await self.rows()) + 2

    async def write(self, start: int, rows: list[list]):
        await self._run(self._write, start, rows)

    def _write(self, start: int, rows: list[list]):
        end = start + len(rows) - 1
        if end > self.ws.row_count:
            self.ws.resize(rows=end + 500)
        if self.ws.row_values(1)[:5] != HEADERS:
            raise SheetConflict("Заголовки таблицы изменены")
        target = f"A{start}:E{end}"
        current = self.ws.get(target, value_render_option="FORMULA")
        # Retrying the same range is idempotent if a previous request committed
        # but its HTTP response was lost. Never reserve a new range on retry.
        for i, expected in enumerate(rows):
            actual = list(current[i]) if i < len(current) else []
            actual += [""] * (5 - len(actual))
            if any(x != "" and x is not None for x in actual) and actual != expected:
                raise SheetConflict(f"Изменены зарезервированные строки {start}–{end}")
        if [list(x) for x in current] == rows:
            return
        self.ws.update(range_name=target, values=rows, value_input_option="RAW")

    async def close(self):
        if self.client is not None:
            await self._run(self.client.http_client.session.close)
        self.executor.shutdown(wait=True)
