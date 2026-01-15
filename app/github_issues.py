import httpx


async def create_github_issue(token: str, repo: str, title: str, body: str):
    """
    repo format: owner/repo
    """
    url = f"https://api.github.com/repos/{repo}/issues"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "k8s-ai-triage",
    }
    payload = {"title": title, "body": body}

    async with httpx.AsyncClient(timeout=20) as x:
        r = await x.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        return data.get("html_url")
