import base64

from openai import AsyncOpenAI

from .config import Settings
from .models import InvalidExtraction, parse_expenses
from .prompts import make_prompt


class Recognizer:
    def __init__(self, settings: Settings):
        self.settings = settings
        # Retry policy is owned by the durable job queue, not nested SDK retries.
        self.client = AsyncOpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            timeout=settings.openai_timeout_seconds,
            max_retries=0,
        )

    async def extract(self, text: str, photo: bytes | None, mime: str | None):
        content = [{"type": "input_text", "text": text or "Распознай расходы на фото."}]
        if photo:
            encoded = base64.b64encode(photo).decode("ascii")
            content.append(
                {
                    "type": "input_image",
                    "detail": "high",
                    "image_url": f"data:{mime};base64,{encoded}",
                }
            )
        response = await self.client.responses.create(
            model=self.settings.openai_model,
            instructions=make_prompt(self.settings.currency, self.settings.max_expenses),
            input=[{"role": "user", "content": content}],
            temperature=0,
            max_output_tokens=12000,
            store=False,
        )
        if response.status != "completed" or not response.output_text:
            raise InvalidExtraction("AI не вернул полный результат")
        # Preserve the requested top-level array. Do not pretend JSON-object mode
        # guarantees arrays: strictly validate the entire plain-text response.
        return parse_expenses(response.output_text, self.settings.max_expenses)

    async def close(self):
        await self.client.close()
