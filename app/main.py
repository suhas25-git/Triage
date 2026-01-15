import os
import json
import uuid
import datetime as dt
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.collectors import (
    k8s_collect_evidence,
    prom_collect_metrics,
    loki_collect_logs,
)
from app.triage import bedrock_triage_markdown
from app.storage import s3_put_json, s3_put_text
from app.github_issues import create_github_issue
from app.slack import send_slack_webhook


APP = FastAPI(title="k8s-ai-incident-triage", version="1.0.0")

# Config from env (ConfigMap + Secret)
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
S3_BUCKET = os.getenv("S3_BUCKET", "")
BEDROCK_MODEL_ID = os.getenv(
    "BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "")
LOKI_URL = os.getenv("LOKI_URL", "")

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # owner/repo


@APP.get("/health")
def health():
    return {"ok": True}


def _now_utc_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _safe_get(d: Dict[str, Any], path: List[str], default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def _extract_targets_from_alert(alert: Dict[str, Any]) -> Dict[str, Optional[str]]:
    labels = alert.get("labels", {}) or {}
    annotations = alert.get("annotations", {}) or {}
    return {
        "alertname": labels.get("alertname"),
        "severity": labels.get("severity"),
        "namespace": labels.get("namespace") or labels.get("kubernetes_namespace"),
        "pod": labels.get("pod") or labels.get("pod_name"),
        "container": labels.get("container"),
        "node": labels.get("node") or labels.get("instance"),
        "summary": annotations.get("summary"),
        "description": annotations.get("description"),
    }


@APP.post("/alert")
async def alertmanager_webhook(req: Request):
    """
    Receives Alertmanager webhook payload.
    Collects evidence (K8s + Prom + Loki), calls Bedrock LLM, sends Slack,
    creates GitHub issue, stores everything to S3.
    """
    body = await req.json()
    status = body.get("status", "unknown")
    alerts = body.get("alerts", []) or []

    incident_id = str(uuid.uuid4())
    created_at = _now_utc_iso()

    # Basic info
    alert_summaries = [_extract_targets_from_alert(a) for a in alerts]
    primary = alert_summaries[0] if alert_summaries else {}

    # Evidence collection
    namespace = primary.get("namespace")
    pod = primary.get("pod")
    node = primary.get("node")

    k8s_evidence = await k8s_collect_evidence(namespace=namespace, pod=pod, node=node)

    prom_evidence = {}
    if PROMETHEUS_URL:
        prom_evidence = await prom_collect_metrics(
            prometheus_url=PROMETHEUS_URL,
            namespace=namespace,
            pod=pod,
            node=node,
        )

    loki_evidence = {}
    if LOKI_URL and namespace and pod:
        loki_evidence = await loki_collect_logs(
            loki_url=LOKI_URL,
            namespace=namespace,
            pod=pod,
            minutes=20,
        )

    evidence_bundle = {
        "incident_id": incident_id,
        "created_at": created_at,
        "alertmanager_status": status,
        "alerts": alert_summaries,
        "k8s": k8s_evidence,
        "prometheus": prom_evidence,
        "loki": loki_evidence,
    }

    # LLM triage
    triage_md = await bedrock_triage_markdown(
        region=AWS_REGION,
        model_id=BEDROCK_MODEL_ID,
        evidence=evidence_bundle,
    )

    # Store to S3 (optional but recommended)
    s3_prefix = f"incidents/{created_at.replace(':','-')}_{incident_id}"
    s3_urls = {}
    if S3_BUCKET:
        await s3_put_json(bucket=S3_BUCKET, key=f"{s3_prefix}/evidence.json", data=evidence_bundle)
        await s3_put_text(bucket=S3_BUCKET, key=f"{s3_prefix}/runbook.md", text=triage_md)
        s3_urls = {
            "evidence_key": f"{s3_prefix}/evidence.json",
            "runbook_key": f"{s3_prefix}/runbook.md",
        }

    # GitHub issue
    issue_url = None
    if GITHUB_TOKEN and GITHUB_REPO:
        title = f"[{primary.get('severity','info')}] {primary.get('alertname','Alert')} - {namespace}/{pod or node or 'unknown'}"
        issue_body = (
            f"### Incident ID\n`{incident_id}`\n\n"
            f"### Created\n{created_at}\n\n"
            f"### Alerts\n```json\n{json.dumps(alert_summaries, indent=2)}\n```\n\n"
            f"### Runbook (AI)\n{triage_md}\n\n"
        )
        if S3_BUCKET and s3_urls:
            issue_body += (
                f"\n### Artifacts (S3)\n"
                f"- evidence: `s3://{S3_BUCKET}/{s3_urls['evidence_key']}`\n"
                f"- runbook:  `s3://{S3_BUCKET}/{s3_urls['runbook_key']}`\n"
            )
        issue_url = await create_github_issue(
            token=GITHUB_TOKEN,
            repo=GITHUB_REPO,
            title=title,
            body=issue_body,
        )

    # Slack notification
    if SLACK_WEBHOOK_URL:
        short = (
            f"*K8s Incident Triage*\n"
            f"*Status:* `{status}`\n"
            f"*Alert:* `{primary.get('alertname','')}`  *Severity:* `{primary.get('severity','')}`\n"
            f"*Target:* `{namespace or '-'} / {pod or node or '-'}`\n"
            f"*Incident ID:* `{incident_id}`\n"
        )
        if issue_url:
            short += f"*GitHub Issue:* {issue_url}\n"
        if S3_BUCKET and s3_urls:
            short += f"*S3:* `s3://{S3_BUCKET}/{s3_urls['runbook_key']}`\n"

        # Include first ~20 lines of runbook for Slack readability
        excerpt = "\n".join(triage_md.splitlines()[:20])
        slack_text = f"{short}\n```{excerpt}```"
        await send_slack_webhook(SLACK_WEBHOOK_URL, slack_text)

    return JSONResponse(
        {
            "ok": True,
            "incident_id": incident_id,
            "issue_url": issue_url,
            "s3": s3_urls,
        }
    )
