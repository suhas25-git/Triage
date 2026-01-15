import os
import json
import datetime as dt
from typing import Any, Dict, Optional

import httpx
from kubernetes import client, config


def _load_k8s():
    """
    Runs in-cluster using ServiceAccount token.
    If you test locally later, it falls back to kubeconfig.
    """
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()


async def k8s_collect_evidence(namespace: Optional[str], pod: Optional[str], node: Optional[str]) -> Dict[str, Any]:
    _load_k8s()
    v1 = client.CoreV1Api()
    apps = client.AppsV1Api()

    evidence: Dict[str, Any] = {
        "namespace": namespace, "pod": pod, "node": node}

    # Node details (if node provided)
    if node:
        try:
            n = v1.read_node(name=node)
            evidence["node_info"] = {
                "name": n.metadata.name,
                "labels": n.metadata.labels,
                "conditions": [{"type": c.type, "status": c.status, "reason": c.reason, "message": c.message} for c in (n.status.conditions or [])],
            }
        except Exception as e:
            evidence["node_info_error"] = str(e)

    # Pod details + events + logs
    if namespace and pod:
        try:
            p = v1.read_namespaced_pod(name=pod, namespace=namespace)
            evidence["pod_info"] = {
                "name": p.metadata.name,
                "namespace": p.metadata.namespace,
                "node_name": p.spec.node_name,
                "labels": p.metadata.labels,
                "phase": p.status.phase,
                "conditions": [{"type": c.type, "status": c.status, "reason": c.reason, "message": c.message} for c in (p.status.conditions or [])],
                "container_statuses": [
                    {
                        "name": cs.name,
                        "ready": cs.ready,
                        "restart_count": cs.restart_count,
                        "state": cs.state.to_dict() if cs.state else None,
                        "last_state": cs.last_state.to_dict() if cs.last_state else None,
                    }
                    for cs in (p.status.container_statuses or [])
                ],
            }
        except Exception as e:
            evidence["pod_info_error"] = str(e)

        # Events
        try:
            ev = v1.list_namespaced_event(
                namespace=namespace,
                field_selector=f"involvedObject.name={pod}",
                limit=50
            )
            evidence["events"] = [
                {
                    "type": i.type,
                    "reason": i.reason,
                    "message": i.message,
                    "first_timestamp": str(i.first_timestamp),
                    "last_timestamp": str(i.last_timestamp),
                }
                for i in (ev.items or [])
            ]
        except Exception as e:
            evidence["events_error"] = str(e)

        # Logs (try each container)
        try:
            p = v1.read_namespaced_pod(name=pod, namespace=namespace)
            logs = {}
            for c in (p.spec.containers or []):
                cname = c.name
                try:
                    text = v1.read_namespaced_pod_log(
                        name=pod,
                        namespace=namespace,
                        container=cname,
                        tail_lines=200,
                        timestamps=True,
                    )
                    logs[cname] = text
                except Exception as e:
                    logs[cname] = f"<log_error> {e}"
            evidence["logs"] = logs
        except Exception as e:
            evidence["logs_error"] = str(e)

    return evidence


async def prom_collect_metrics(prometheus_url: str, namespace: Optional[str], pod: Optional[str], node: Optional[str]) -> Dict[str, Any]:
    """
    Minimal useful metrics evidence:
    - CPU rate for pod
    - Memory working set for pod
    - Node readiness condition already captured in kube-state-metrics alert, but add node CPU usage if node known.
    """
    out: Dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=20) as x:
        # Pod CPU
        if namespace and pod:
            q_cpu = f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",pod="{pod}",container!=""}}[5m]))'
            r = await x.get(f"{prometheus_url}/api/v1/query", params={"query": q_cpu})
            out["pod_cpu_query"] = q_cpu
            out["pod_cpu"] = r.json()

            # Pod memory
            q_mem = f'sum(container_memory_working_set_bytes{{namespace="{namespace}",pod="{pod}",container!=""}})'
            r2 = await x.get(f"{prometheus_url}/api/v1/query", params={"query": q_mem})
            out["pod_mem_query"] = q_mem
            out["pod_memory"] = r2.json()

        # Node CPU (optional)
        if node:
            q_ncpu = f'sum(rate(node_cpu_seconds_total{{instance=~"{node}.*",mode!="idle"}}[5m]))'
            r3 = await x.get(f"{prometheus_url}/api/v1/query", params={"query": q_ncpu})
            out["node_cpu_query"] = q_ncpu
            out["node_cpu"] = r3.json()

    return out


async def loki_collect_logs(loki_url: str, namespace: str, pod: str, minutes: int = 20) -> Dict[str, Any]:
    """
    Loki query for recent logs for the pod.
    """
    end = dt.datetime.utcnow()
    start = end - dt.timedelta(minutes=minutes)
    # Loki uses nanoseconds epoch for query_range
    start_ns = int(start.timestamp() * 1e9)
    end_ns = int(end.timestamp() * 1e9)

    # Common Loki labels for promtail on k8s: namespace, pod
    query = f'{{namespace="{namespace}", pod="{pod}"}}'
    params = {
        "query": query,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": "200",
        "direction": "BACKWARD",
    }

    async with httpx.AsyncClient(timeout=20) as x:
        r = await x.get(f"{loki_url}/loki/api/v1/query_range", params=params)
        return {"query": query, "range_minutes": minutes, "response": r.json()}
