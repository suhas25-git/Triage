import httpx


async def send_slack_webhook(webhook_url: str, text: str) -> None:
    async with httpx.AsyncClient(timeout=20) as x:
        r = await x.post(webhook_url, json={"text": text})
        r.raise_for_status()
