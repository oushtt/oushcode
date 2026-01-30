from __future__ import annotations

import html
import json
import os
from typing import Iterable

from agent.storage.db import Job


def _read_transcript(artifacts_dir: str, job_id: int) -> str:
    path = os.path.join(artifacts_dir, f"job-{job_id}", "transcript.md")
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _read_events(artifacts_dir: str, job_id: int, limit: int = 200) -> list[dict[str, object]]:
    path = os.path.join(artifacts_dir, f"job-{job_id}", "events.jsonl")
    if not os.path.exists(path):
        return []
    items: list[dict[str, object]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items[-limit:]


def _job_label(job: Job) -> str:
    suffix = ""
    if job.issue_number:
        suffix = f"issue #{job.issue_number}"
    elif job.pr_number:
        suffix = f"pr #{job.pr_number}"
    return f"{job.kind} · {suffix}" if suffix else job.kind


def render_ui(
    *,
    jobs: Iterable[Job],
    selected: Job | None,
    artifacts_dir: str,
    status_filter: str | None = None,
) -> str:
    jobs_list = list(jobs)
    if status_filter and status_filter != "all":
        jobs_list = [j for j in jobs_list if j.status == status_filter]
    jobs_list.sort(key=lambda j: j.id, reverse=True)

    selected_job = selected or (jobs_list[0] if jobs_list else None)
    transcript = _read_transcript(artifacts_dir, selected_job.id) if selected_job else ""
    events = _read_events(artifacts_dir, selected_job.id) if selected_job else []

    def esc(text: str) -> str:
        return html.escape(text)

    def badge(status: str) -> str:
        cls = {
            "queued": "badge queued",
            "running": "badge running",
            "done": "badge done",
            "failed": "badge failed",
        }.get(status, "badge")
        return f"<span class=\"{cls}\">{esc(status)}</span>"

    def filter_link(label: str, value: str) -> str:
        cls = "filter active" if status_filter == value else "filter"
        return f"<a class=\"{cls}\" href=\"/ui?status={value}\">{label}</a>"

    filters_html = (
        filter_link("All", "all")
        + filter_link("Queued", "queued")
        + filter_link("Running", "running")
        + filter_link("Done", "done")
        + filter_link("Failed", "failed")
    )

    jobs_html = "".join(
        [
            """
            <a class="job" href="/ui?job_id={id}">
              <div class="job-head">
                <div class="job-id">#{id}</div>
                {status}
              </div>
              <div class="job-title">{label}</div>
              <div class="job-meta">{repo}</div>
              <div class="job-meta small">{updated}</div>
            </a>
            """.format(
                id=j.id,
                status=badge(j.status),
                label=esc(_job_label(j)),
                repo=esc(j.repo or "-") ,
                updated=esc(j.updated_at),
            )
            for j in jobs_list
        ]
    )

    events_html = "".join(
        [
            """
            <div class="event">
              <div class="event-meta">{ts} · {kind}</div>
              <div class="event-msg">{msg}</div>
            </div>
            """.format(
                ts=esc(str(e.get("ts", ""))),
                kind=esc(str(e.get("kind", ""))),
                msg=esc(str(e.get("message", ""))),
            )
            for e in events
        ]
    )

    transcript_html = "<pre class=\"transcript\">{}</pre>".format(esc(transcript))

    selected_block = ""
    if selected_job:
        selected_block = f"""
        <div class=\"selected\">
          <div class=\"selected-title\">Job #{selected_job.id}</div>
          <div class=\"selected-meta\">{esc(selected_job.kind)} · {esc(selected_job.repo or '-')}
            <span class=\"dot\">•</span> {esc(selected_job.created_at)}
          </div>
          <div class=\"selected-badges\">{badge(selected_job.status)}</div>
        </div>
        """

    return f"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>OushCode Console</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=Chivo+Mono:wght@300;500&display=swap');
:root {{
  --bg: #0c1015;
  --panel: #121824;
  --panel-2: #0e141e;
  --accent: #ffb454;
  --accent-2: #7bf1a8;
  --muted: #9aa4b2;
  --text: #e6edf3;
  --danger: #ff6b6b;
  --warn: #ffd166;
  --ok: #7bf1a8;
  --shadow: rgba(0,0,0,0.35);
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  color: var(--text);
  background: radial-gradient(1200px 800px at 10% -10%, #1a2331 0%, #0c1015 60%);
  font-family: 'Chivo Mono', monospace;
}}
.header {{
  padding: 24px 28px;
  border-bottom: 1px solid #202938;
  background: linear-gradient(90deg, #121824 0%, #0e141e 60%);
}}
.title {{
  font-family: 'Fraunces', serif;
  font-size: 28px;
  letter-spacing: 0.5px;
}}
.subtitle {{
  color: var(--muted);
  font-size: 12px;
}}
.layout {{
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 16px;
  padding: 16px;
}}
.panel {{
  background: var(--panel);
  border: 1px solid #1f2a3b;
  border-radius: 16px;
  box-shadow: 0 12px 30px var(--shadow);
  overflow: hidden;
}}
.queue {{
  display: flex;
  flex-direction: column;
  max-height: calc(100vh - 120px);
  overflow: auto;
}}
.filters {{
  display: flex;
  gap: 8px;
  padding: 12px 14px;
  border-bottom: 1px solid #1f2a3b;
  position: sticky;
  top: 0;
  background: var(--panel);
  z-index: 2;
}}
.filter {{
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
  border: 1px solid #2a3344;
  padding: 4px 8px;
  border-radius: 999px;
  text-decoration: none;
}}
.filter.active {{
  color: #111827;
  background: var(--accent);
  border-color: #62411f;
}}
.job {{
  display: block;
  text-decoration: none;
  color: inherit;
  padding: 14px 16px;
  border-bottom: 1px solid #1f2a3b;
  transition: background 0.2s ease;
}}
.job:hover {{ background: #182233; }}
.job-head {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}}
.job-title {{
  font-size: 13px;
  margin-bottom: 6px;
}}
.job-meta {{
  color: var(--muted);
  font-size: 11px;
}}
.job-meta.small {{ font-size: 10px; }}
.badge {{
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 10px;
  border: 1px solid #2a3344;
  text-transform: uppercase;
}}
.badge.queued {{ color: var(--warn); border-color: #4a3f1e; }}
.badge.running {{ color: var(--accent); border-color: #4a3419; }}
.badge.done {{ color: var(--ok); border-color: #1f4d2f; }}
.badge.failed {{ color: var(--danger); border-color: #4a1e1e; }}
.content {{
  display: grid;
  grid-template-rows: auto 1fr;
  gap: 12px;
}}
.selected {{
  padding: 18px;
  background: var(--panel-2);
  border: 1px solid #202938;
  border-radius: 16px;
}}
.selected-title {{
  font-family: 'Fraunces', serif;
  font-size: 20px;
}}
.selected-meta {{
  color: var(--muted);
  font-size: 12px;
  margin-top: 6px;
}}
.selected-badges {{ margin-top: 10px; }}
.dot {{ color: var(--accent); padding: 0 6px; }}
.grid {{
  display: grid;
  grid-template-columns: 1.2fr 0.8fr;
  gap: 12px;
  height: calc(100vh - 220px);
}}
.box {{
  padding: 16px;
  border-radius: 16px;
  border: 1px solid #1f2a3b;
  background: #101724;
  min-height: 240px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}}
.box h3 {{
  margin: 0 0 10px 0;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--muted);
}}
.event {{
  padding: 10px 0;
  border-bottom: 1px dashed #223146;
}}
.event-meta {{ color: var(--muted); font-size: 10px; }}
.event-msg {{ font-size: 12px; margin-top: 4px; }}
.transcript {{
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 1.5;
  overflow: auto;
  padding-right: 8px;
  flex: 1;
}}
.box-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
}}
.copy-btn {{
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
  border: 1px solid #2a3344;
  padding: 4px 8px;
  border-radius: 999px;
  background: transparent;
  cursor: pointer;
}}
.copy-btn:hover {{
  color: #111827;
  background: var(--accent);
  border-color: #62411f;
}}
@media (max-width: 980px) {{
  .layout {{ grid-template-columns: 1fr; }}
  .grid {{ grid-template-columns: 1fr; }}
  .queue {{ max-height: 280px; }}
}}
</style>
</head>
<body>
  <script>
    const autoRefresh = true;
    if (autoRefresh) {{
      setInterval(() => {{
        const url = new URL(window.location.href);
        fetch(url.toString(), {{ headers: {{ "X-UI-Refresh": "1" }} }})
          .then(r => r.text())
          .then(html => {{
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, "text/html");
            const nextQueue = doc.querySelector(".queue");
            const nextContent = doc.querySelector(".content");
            if (nextQueue && nextContent) {{
              document.querySelector(".queue").innerHTML = nextQueue.innerHTML;
              document.querySelector(".content").innerHTML = nextContent.innerHTML;
            }}
          }})
          .catch(() => {{}});
      }}, 5000);
    }}
  </script>
  <div class="header">
    <div class="title">Agent Console</div>
    <div class="subtitle">Queue · Jobs · Artifacts</div>
  </div>
  <div class="layout">
    <div class="panel queue">
      <div class="filters">
        {filters_html}
      </div>
      {jobs_html if jobs_html else '<div class="job">No jobs yet</div>'}
    </div>
    <div class="content">
      {selected_block}
      <div class="grid">
        <div class="box">
          <div class="box-header">
            <h3>Transcript</h3>
            <button class="copy-btn" onclick="copyTranscript()">Copy</button>
          </div>
          {transcript_html}
        </div>
        <div class="box">
          <div class="box-header">
            <h3>Events</h3>
          </div>
          {events_html if events_html else '<div class="event">No events yet</div>'}
        </div>
      </div>
    </div>
  </div>
  <script>
    function copyTranscript() {{
      const text = document.querySelector('.transcript')?.innerText || '';
      navigator.clipboard.writeText(text);
    }}
  </script>
</body>
</html>
"""
