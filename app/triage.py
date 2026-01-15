import json
from typing import Any, Dict

import boto3


SYSTEM_STYLE = """You are an SRE incident triage assistant for Kubernetes.
You will be given evidence from Kubernetes API, Prometheus, and Loki.
Your job:
1) Identify the most probable root cause
2) Provide step-by-step fix
3) Provide rollback plan
4) Provide a short runbook in Markdown
Be concrete, safe, and actionable. Avoid guessing; if uncertain, list top 2 hypotheses with checks."""

OUTPUT_FORMAT = """Return Markdown with these sections exactly:
## Summary
## Probable Root Cause
## Evidence
## Step-by-step Fix
## Rollback Plan
## Verification
## Preventive Actions
"""


def _build_prompt(evidence: Dict[str, Any]) -> str:
    # Keep prompt bounded. We include key parts, and trim large logs if needed.
    safe = json.dumps(evidence, indent=2)
    if len(safe) > 120000:
        safe = safe[:120000] + "\n...<truncated>..."
    return f"{SYSTEM_STYLE}\n\n{OUTPUT_FORMAT}\n\n### Evidence JSON\n```json\n{safe}\n```"


async def bedrock_triage_markdown(region: str, model_id: str, evidence: Dict[str, Any]) -> str:
    """
    Uses Bedrock Runtime invoke_model.
    This implementation targets Anthropic Claude models on Bedrock.
    If you change to a different model family later, adjust request format.
    """
    client = boto3.client("bedrock-runtime", region_name=region)
    prompt = _build_prompt(evidence)

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 900,
        "temperature": 0.2,
        "messages": [
            {"role": "user", "content": prompt}
        ],
    }

    resp = client.invoke_model(
        modelId=model_id,
        body=json.dumps(body).encode("utf-8"),
        contentType="application/json",
        accept="application/json",
    )
    payload = json.loads(resp["body"].read())

    # Claude on Bedrock returns content array
    content = payload.get("content", [])
    text_parts = []
    for c in content:
        if c.get("type") == "text":
            text_parts.append(c.get("text", ""))
    out = "\n".join(text_parts).strip()
    return out or "## Summary\nNo response from model."
