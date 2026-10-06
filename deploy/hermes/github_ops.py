"""Task-local GitHub readback/download helper; never prints credentials."""
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx

REPO = "edwin-hou/gov-contract-ipo-monitor"
BASE = "https://api.github.com/repos/" + REPO


def headers():
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    result = subprocess.run(
        [r"C:\Program Files\Git\cmd\git.exe", "-c", "credential.interactive=never", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n", text=True, capture_output=True,
        env=environment, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True,
    )
    credential = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return {"Authorization": "Bearer " + credential["password"], "User-Agent": "ipo-monitor-verification", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def api(path):
    response = httpx.get(BASE + path, headers=headers(), timeout=30)
    response.raise_for_status()
    return response.json()


if sys.argv[1] == "dispatch":
    response = httpx.post(BASE + "/actions/workflows/monitor.yml/dispatches", headers=headers(), json={"ref": "main"}, timeout=30)
    response.raise_for_status()
    print("Requested one verification collection on current main.")
elif sys.argv[1] == "runs":
    path = "/actions/runs?per_page=15"
    if len(sys.argv) > 2:
        path += "&head_sha=" + sys.argv[2]
    data = api(path)
    print(json.dumps([{k: item.get(k) for k in ("id", "name", "event", "status", "conclusion", "head_sha", "head_branch", "created_at", "html_url")} for item in data["workflow_runs"]], indent=2))
elif sys.argv[1] == "main":
    data = api("/branches/main")
    print(json.dumps({"branch": data["name"], "sha": data["commit"]["sha"]}, indent=2))
elif sys.argv[1] == "workflow":
    data = api("/actions/workflows/monitor.yml")
    print(json.dumps({key: data[key] for key in ("id", "name", "path", "state", "html_url")}, indent=2))
elif sys.argv[1] == "run":
    data = api("/actions/runs/" + sys.argv[2])
    print(json.dumps({key: data.get(key) for key in ("id", "name", "path", "status", "conclusion", "head_sha", "head_branch", "html_url", "created_at", "run_started_at", "updated_at", "check_suite_id")}, indent=2))
elif sys.argv[1] == "jobs":
    data = api("/actions/runs/" + sys.argv[2] + "/jobs")
    print(json.dumps([{key: item.get(key) for key in ("id", "name", "status", "conclusion", "started_at", "completed_at", "runner_name", "steps")} for item in data["jobs"]], indent=2))
elif sys.argv[1] == "checks":
    data = api("/check-suites/" + sys.argv[2] + "/check-runs")
    print(json.dumps([{key: item.get(key) for key in ("id", "name", "status", "conclusion", "output")} for item in data["check_runs"]], indent=2))
    for item in data["check_runs"]:
        annotations = api("/check-runs/" + str(item["id"]) + "/annotations")
        print(json.dumps(annotations, indent=2))
elif sys.argv[1] == "artifacts":
    data = api("/actions/runs/" + sys.argv[2] + "/artifacts")
    print(json.dumps([{key: item.get(key) for key in ("id", "name", "size_in_bytes", "expired", "created_at", "updated_at", "workflow_run")} for item in data["artifacts"]], indent=2))
elif sys.argv[1] == "job-log":
    response = httpx.get(BASE + "/actions/jobs/" + sys.argv[2] + "/logs", headers=headers(), timeout=30, follow_redirects=False)
    if response.status_code in (301, 302, 303, 307, 308):
        redirect = httpx.URL(response.headers["location"])
        if redirect.scheme != "https" or not redirect.host.endswith((".blob.core.windows.net", ".githubusercontent.com")):
            raise ValueError("Unexpected GitHub job log download host")
        response = httpx.get(str(redirect), timeout=30)
    response.raise_for_status()
    if len(response.content) > 25_000_000:
        raise ValueError("Job log exceeds task readback bound")
    destination = Path(sys.argv[3])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(response.text, encoding="utf-8")
    print("Saved job log to " + str(destination))
elif sys.argv[1] == "download":
    identifier, destination = sys.argv[2], Path(sys.argv[3])
    response = httpx.get(BASE + "/actions/artifacts/" + identifier + "/zip", headers=headers(), timeout=30, follow_redirects=False)
    if response.status_code in (301, 302, 303, 307, 308):
        redirect = httpx.URL(response.headers["location"])
        if redirect.scheme != "https" or not redirect.host.endswith((".blob.core.windows.net", ".githubusercontent.com")):
            raise ValueError("Unexpected GitHub artifact download host")
        response = httpx.get(str(redirect), timeout=120)
        response.raise_for_status()
    else:
        response.raise_for_status()
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        for item in bundle.infolist():
            if item.filename not in {"latest.json", "latest.md", "monitor.db"} or item.file_size > 250_000_000:
                raise ValueError("Unexpected artifact file")
            (destination / item.filename).write_bytes(bundle.read(item))
    report = destination / "latest.json"
    if report.exists():
        data = json.loads(report.read_text(encoding="utf-8"))
        print(json.dumps({key: data[key] for key in ("completed_at", "status", "counts", "ipo_summary")}, indent=2))
    else:
        print("Saved verified checkpoint to " + str(destination))
else:
    raise ValueError("Unknown operation")
