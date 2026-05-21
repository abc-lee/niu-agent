"""
HTTP Log API — FastAPI router that serves LLM HTTP request logs
with an embedded HTML viewer.

Log files are stored at: logs/raw_http/{YYYYMMDD}/{seq:06d}.json
Each JSON file contains one request/response pair.
"""

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(prefix="/http-log", tags=["http-log"])

# Root logs directory
_LOG_DIR = Path("logs") / "raw_http"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def viewer_page():
    """Serve the embedded HTML log viewer."""
    return HTMLResponse(content=_HTML_VIEWER)


@router.get("/dates", response_class=JSONResponse)
def list_dates():
    """Return a list of dates that have log entries, newest first."""
    if not _LOG_DIR.exists():
        return []
    dates = sorted(
        (d.name for d in _LOG_DIR.iterdir()
         if d.is_dir() and d.name.isdigit() and len(d.name) == 8),
        reverse=True,
    )
    return dates


@router.get("/{date}/entries", response_class=JSONResponse)
def list_entries(date: str):
    """Return summary list for all entries of a given date.

    Supports two log formats:
    - Transport layer: {seq:06d}.json (request + streaming response marker)
    - Application layer: {seq:06d}_request.json + {seq:06d}_response.json
    """
    day_dir = _LOG_DIR / date
    if not day_dir.is_dir():
        return []

    # Collect all seq numbers from all file types
    seqs: set[int] = set()
    for f in day_dir.glob("*.json"):
        try:
            # "000001" or "000001_request" or "000001_response"
            seq_str = f.stem.split("_")[0]
            seqs.add(int(seq_str))
        except ValueError:
            continue

    summaries: list[dict] = []
    for seq in sorted(seqs):
        summary = _build_summary(day_dir, seq)
        if summary:
            summaries.append(summary)
    return summaries


def _build_summary(day_dir: Path, seq: int) -> dict | None:
    """Build a summary for a single seq by merging available log files."""
    transport_file = day_dir / f"{seq:06d}.json"
    request_file = day_dir / f"{seq:06d}_request.json"
    response_file = day_dir / f"{seq:06d}_response.json"

    summary: dict = {"seq": seq}

    # Prefer application-layer request (has full messages without truncation)
    req_data = None
    if request_file.is_file():
        try:
            req_data = json.loads(request_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    if req_data:
        summary["timestamp"] = req_data.get("timestamp")
        summary["model"] = req_data.get("model")
        msgs = req_data.get("messages")
        if isinstance(msgs, list):
            summary["msg_count"] = len(msgs)
        tools = req_data.get("tools")
        if isinstance(tools, list):
            summary["tool_count"] = len(tools)

    # Prefer application-layer response (has full content)
    resp_data = None
    if response_file.is_file():
        try:
            resp_data = json.loads(response_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    if resp_data:
        if "timestamp" not in summary:
            summary["timestamp"] = resp_data.get("timestamp")
        if "model" not in summary:
            summary["model"] = resp_data.get("model")
        usage = resp_data.get("usage")
        if isinstance(usage, dict):
            summary["prompt_tokens"] = usage.get("prompt_tokens")
            summary["completion_tokens"] = usage.get("completion_tokens")
            summary["total_tokens"] = usage.get("total_tokens")
        # Mark streaming if response has content (means it was captured)
        summary["streaming"] = False

    # Fall back to transport-layer data for fields not yet filled
    if transport_file.is_file():
        try:
            transport_data = json.loads(transport_file.read_text(encoding="utf-8"))
        except Exception:
            transport_data = None
        if transport_data:
            if "timestamp" not in summary:
                summary["timestamp"] = transport_data.get("timestamp")
            summary["elapsed_ms"] = transport_data.get("elapsed_ms")
            req = transport_data.get("request", {})
            if "model" not in summary:
                body = req.get("body")
                if isinstance(body, dict):
                    summary["model"] = body.get("model")
            summary["method"] = req.get("method")
            summary["url"] = req.get("url")
            resp = transport_data.get("response", {})
            summary["status_code"] = resp.get("status_code")
            if "streaming" not in summary:
                body = resp.get("body")
                if isinstance(body, dict):
                    summary["streaming"] = body.get("streaming", False)

    if not req_data and not resp_data and not transport_file.is_file():
        return None
    return summary


@router.get("/{date}/entries/{seq}", response_class=JSONResponse)
def get_entry(date: str, seq: int):
    """Return the full JSON data for a single log entry.

    Merges transport-layer and application-layer data.
    """
    day_dir = _LOG_DIR / date
    transport_file = day_dir / f"{seq:06d}.json"
    request_file = day_dir / f"{seq:06d}_request.json"
    response_file = day_dir / f"{seq:06d}_response.json"

    if not transport_file.is_file() and not request_file.is_file() and not response_file.is_file():
        raise HTTPException(status_code=404, detail="Entry not found")

    result: dict = {"seq": seq}

    # Transport layer data
    if transport_file.is_file():
        try:
            result["transport"] = json.loads(transport_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Application layer request
    if request_file.is_file():
        try:
            result["request"] = json.loads(request_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Application layer response
    if response_file.is_file():
        try:
            result["response"] = json.loads(response_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Embedded HTML Viewer
# ---------------------------------------------------------------------------

_HTML_VIEWER = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LLM HTTP Log Viewer</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg: #1a1a2e;
  --card: #16213e;
  --card-hover: #1a2744;
  --accent: #e94560;
  --accent-dim: #c73652;
  --text: #e0e0e0;
  --text-dim: #8a8a9a;
  --border: #2a2a4a;
  --blue-bg: #e3f2fd;
  --green-bg: #e8f5e9;
  --gray-bg: #f5f5f5;
  --yellow-bg: #fff8e1;
  --orange: #ff9800;
  --success: #4caf50;
  --error: #f44336;
  --radius: 6px;
}

html, body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
}

/* ---- Top Nav ---- */
.navbar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 24px;
  background: var(--card);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 100;
}
.navbar h1 {
  font-size: 18px;
  font-weight: 600;
  white-space: nowrap;
}
.navbar select {
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 6px 12px;
  font-size: 14px;
  cursor: pointer;
  min-width: 140px;
}
.navbar button {
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: var(--radius);
  padding: 6px 16px;
  font-size: 14px;
  cursor: pointer;
  transition: background .15s;
}
.navbar button:hover { background: var(--accent-dim); }

/* ---- Stats Bar ---- */
.stats-bar {
  display: flex;
  gap: 24px;
  padding: 12px 24px;
  background: var(--card);
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  color: var(--text-dim);
  flex-wrap: wrap;
}
.stats-bar .stat-val {
  color: var(--text);
  font-weight: 600;
  margin-left: 4px;
}

/* ---- Table ---- */
.table-wrap {
  padding: 16px 24px;
  overflow-x: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
thead th {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 2px solid var(--border);
  color: var(--text-dim);
  font-weight: 600;
  white-space: nowrap;
}
tbody tr {
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  transition: background .12s;
}
tbody tr:hover { background: var(--card-hover); }
tbody tr.active { background: var(--card); }
tbody td {
  padding: 8px 10px;
  white-space: nowrap;
}
.tag-stream {
  display: inline-block;
  background: var(--orange);
  color: #000;
  font-size: 11px;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: 3px;
  margin-left: 4px;
}
.status-ok { color: var(--success); }
.status-err { color: var(--error); }

/* ---- Detail Panel ---- */
.detail-panel {
  margin: 0 24px 24px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  display: none;
}
.detail-panel.open { display: block; }
.detail-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  font-size: 14px;
  font-weight: 600;
}
.detail-header button {
  background: none;
  border: none;
  color: var(--text-dim);
  cursor: pointer;
  font-size: 18px;
}
.detail-body {
  display: flex;
  min-height: 200px;
}
.detail-half {
  flex: 1;
  padding: 12px 16px;
  overflow: auto;
  max-height: 70vh;
}
.detail-half + .detail-half {
  border-left: 1px solid var(--border);
}
.detail-half h3 {
  font-size: 13px;
  color: var(--text-dim);
  margin-bottom: 8px;
  text-transform: uppercase;
  letter-spacing: .5px;
}
.detail-url {
  font-size: 12px;
  word-break: break-all;
  color: var(--accent);
  margin-bottom: 10px;
}

/* ---- Collapsible sections ---- */
.collapsible-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  user-select: none;
  font-size: 13px;
  color: var(--text-dim);
  margin-bottom: 6px;
}
.collapsible-toggle:hover { color: var(--text); }
.collapsible-toggle .sign {
  display: inline-block;
  width: 16px;
  text-align: center;
  font-weight: 700;
}
.collapsible-content {
  margin-left: 22px;
  margin-bottom: 8px;
}

/* ---- JSON Tree ---- */
.json-tree { font-size: 12px; font-family: "SF Mono", "Fira Code", "Consolas", monospace; }
.json-tree .jt-key { color: #82aaff; }
.json-tree .jt-str { color: #c3e88d; }
.json-tree .jt-num { color: #f78c6c; }
.json-tree .jt-bool { color: #c792ea; }
.json-tree .jt-null { color: var(--text-dim); font-style: italic; }
.json-tree .jt-bracket { color: var(--text-dim); }
.json-tree .jt-toggle {
  cursor: pointer;
  user-select: none;
  color: var(--text-dim);
}
.json-tree .jt-toggle:hover { color: var(--text); }
.json-tree .jt-children { margin-left: 16px; }
.json-tree .jt-collapsed-hint { color: var(--text-dim); font-style: italic; }

/* Role highlights */
.msg-block { margin: 4px 0; padding: 6px 8px; border-radius: 4px; }
.msg-role-system { background: var(--blue-bg); color: #1a1a2e; }
.msg-role-user { background: var(--green-bg); color: #1a1a2e; }
.msg-role-assistant { background: var(--gray-bg); color: #1a1a2e; }
.msg-role-tool { background: var(--yellow-bg); color: #1a1a2e; }

/* Long string collapse */
.long-str-toggle {
  color: var(--accent);
  cursor: pointer;
  font-size: 11px;
  margin-left: 4px;
}
.long-str-toggle:hover { text-decoration: underline; }

/* ---- Empty state ---- */
.empty-state {
  text-align: center;
  padding: 60px 24px;
  color: var(--text-dim);
  font-size: 15px;
}

/* ---- Responsive ---- */
@media (max-width: 768px) {
  .detail-body { flex-direction: column; }
  .detail-half + .detail-half { border-left: none; border-top: 1px solid var(--border); }
  .stats-bar { gap: 12px; }
}
</style>
</head>
<body>

<div class="navbar">
  <h1>LLM HTTP Log Viewer</h1>
  <select id="dateSelect"><option value="">-- select date --</option></select>
  <button id="refreshBtn">Refresh</button>
</div>

<div class="stats-bar" id="statsBar" style="display:none">
  <span>Entries:<span class="stat-val" id="statCount">0</span></span>
  <span>Avg elapsed:<span class="stat-val" id="statAvgMs">0</span> ms</span>
  <span>Prompt tokens:<span class="stat-val" id="statPrompt">0</span></span>
  <span>Completion tokens:<span class="stat-val" id="statCompletion">0</span></span>
</div>

<div class="table-wrap" id="tableWrap">
  <div class="empty-state" id="emptyState">Select a date to view logs</div>
  <table id="logTable" style="display:none">
    <thead>
      <tr>
        <th>#</th>
        <th>Time</th>
        <th>Model</th>
        <th>Elapsed (ms)</th>
        <th>Status</th>
        <th>Prompt</th>
        <th>Completion</th>
      </tr>
    </thead>
    <tbody id="logBody"></tbody>
  </table>
</div>

<div class="detail-panel" id="detailPanel">
  <div class="detail-header">
    <span id="detailTitle">Entry Detail</span>
    <button id="detailClose">&times;</button>
  </div>
  <div class="detail-body">
    <div class="detail-half" id="detailRequest">
      <h3>Request</h3>
      <div id="reqContent"></div>
    </div>
    <div class="detail-half" id="detailResponse">
      <h3>Response</h3>
      <div id="respContent"></div>
    </div>
  </div>
</div>

<script>
(function() {
  const dateSelect = document.getElementById('dateSelect');
  const refreshBtn = document.getElementById('refreshBtn');
  const statsBar = document.getElementById('statsBar');
  const logTable = document.getElementById('logTable');
  const logBody = document.getElementById('logBody');
  const emptyState = document.getElementById('emptyState');
  const detailPanel = document.getElementById('detailPanel');
  const detailTitle = document.getElementById('detailTitle');
  const detailClose = document.getElementById('detailClose');
  const reqContent = document.getElementById('reqContent');
  const respContent = document.getElementById('respContent');

  let currentDate = '';
  let currentSeq = null;
  let entriesCache = [];

  // ---- Init ----
  loadDates();

  refreshBtn.addEventListener('click', () => {
    if (currentDate) loadEntries(currentDate);
    else loadDates();
  });

  dateSelect.addEventListener('change', () => {
    const d = dateSelect.value;
    if (d) loadEntries(d);
  });

  detailClose.addEventListener('click', () => {
    detailPanel.classList.remove('open');
    currentSeq = null;
    document.querySelectorAll('#logBody tr.active').forEach(r => r.classList.remove('active'));
  });

  // ---- Load dates ----
  async function loadDates() {
    try {
      const res = await fetch('/http-log/dates');
      const dates = await res.json();
      dateSelect.innerHTML = '<option value="">-- select date --</option>';
      dates.forEach(d => {
        const opt = document.createElement('option');
        opt.value = d;
        opt.textContent = d.slice(0,4) + '-' + d.slice(4,6) + '-' + d.slice(6,8);
        dateSelect.appendChild(opt);
      });
      if (dates.length > 0 && !currentDate) {
        dateSelect.value = dates[0];
        loadEntries(dates[0]);
      }
    } catch(e) {
      console.error('Failed to load dates', e);
    }
  }

  // ---- Load entries ----
  async function loadEntries(date) {
    currentDate = date;
    detailPanel.classList.remove('open');
    currentSeq = null;
    try {
      const res = await fetch('/http-log/' + date + '/entries');
      entriesCache = await res.json();
      renderTable(entriesCache);
      renderStats(entriesCache);
    } catch(e) {
      console.error('Failed to load entries', e);
      entriesCache = [];
      renderTable([]);
    }
  }

  // ---- Render stats ----
  function renderStats(entries) {
    if (entries.length === 0) {
      statsBar.style.display = 'none';
      return;
    }
    statsBar.style.display = 'flex';
    document.getElementById('statCount').textContent = entries.length;
    const avgMs = entries.reduce((s, e) => s + (e.elapsed_ms || 0), 0) / entries.length;
    document.getElementById('statAvgMs').textContent = avgMs.toFixed(0);
    const prompt = entries.reduce((s, e) => s + (e.prompt_tokens || 0), 0);
    document.getElementById('statPrompt').textContent = prompt.toLocaleString();
    const completion = entries.reduce((s, e) => s + (e.completion_tokens || 0), 0);
    document.getElementById('statCompletion').textContent = completion.toLocaleString();
  }

  // ---- Render table ----
  function renderTable(entries) {
    logBody.innerHTML = '';
    if (entries.length === 0) {
      logTable.style.display = 'none';
      emptyState.style.display = 'block';
      emptyState.textContent = currentDate ? 'No entries for this date' : 'Select a date to view logs';
      return;
    }
    emptyState.style.display = 'none';
    logTable.style.display = 'table';
    entries.forEach(e => {
      const tr = document.createElement('tr');
      tr.dataset.seq = e.seq;
      if (e.seq === currentSeq) tr.classList.add('active');

      const time = e.timestamp ? e.timestamp.replace('T', ' ').slice(11, 23) : '-';
      const model = e.model || '-';
      const elapsed = e.elapsed_ms != null ? e.elapsed_ms : '-';
      const status = e.status_code != null ? e.status_code : '-';
      const statusClass = (typeof status === 'number' && status < 400) ? 'status-ok' : (typeof status === 'number' ? 'status-err' : '');
      const streamTag = e.streaming ? '<span class="tag-stream">Stream</span>' : '';
      const prompt = e.prompt_tokens != null ? e.prompt_tokens.toLocaleString() : '-';
      const completion = e.completion_tokens != null ? e.completion_tokens.toLocaleString() : '-';

      tr.innerHTML =
        '<td>' + e.seq + streamTag + '</td>' +
        '<td>' + time + '</td>' +
        '<td>' + escHtml(model) + '</td>' +
        '<td>' + elapsed + '</td>' +
        '<td class="' + statusClass + '">' + status + '</td>' +
        '<td>' + prompt + '</td>' +
        '<td>' + completion + '</td>';

      tr.addEventListener('click', () => toggleDetail(e.seq, tr));
      logBody.appendChild(tr);
    });
  }

  // ---- Toggle detail ----
  async function toggleDetail(seq, tr) {
    if (currentSeq === seq) {
      detailPanel.classList.remove('open');
      currentSeq = null;
      tr.classList.remove('active');
      return;
    }
    document.querySelectorAll('#logBody tr.active').forEach(r => r.classList.remove('active'));
    tr.classList.add('active');
    currentSeq = seq;
    detailTitle.textContent = 'Entry #' + seq;
    reqContent.innerHTML = '<div style="color:var(--text-dim)">Loading...</div>';
    respContent.innerHTML = '';
    detailPanel.classList.add('open');

    try {
      const res = await fetch('/http-log/' + currentDate + '/entries/' + seq);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      renderDetail(data);
    } catch(e) {
      reqContent.innerHTML = '<div style="color:var(--error)">Failed to load: ' + escHtml(e.message) + '</div>';
    }
  }

  // ---- Render detail ----
  function renderDetail(data) {
    const req = data.request || {};
    const resp = data.response || {};

    // Request side
    let reqHtml = '';
    reqHtml += '<div class="detail-url">' + escHtml(req.method || '') + ' ' + escHtml(req.url || '') + '</div>';
    reqHtml += makeCollapsible('Headers', renderHeaders(req.headers), true);
    if (req.body !== undefined) {
      const bodyHtml = renderJsonTree(req.body, 0, true);
      reqHtml += makeCollapsible('Body', bodyHtml, false);
    }
    reqContent.innerHTML = reqHtml;

    // Response side
    let respHtml = '';
    if (resp.status_code != null) {
      const cls = resp.status_code < 400 ? 'status-ok' : 'status-err';
      respHtml += '<div style="margin-bottom:10px">Status: <span class="' + cls + '">' + resp.status_code + '</span></div>';
    }
    respHtml += makeCollapsible('Headers', renderHeaders(resp.headers), true);
    if (resp.body !== undefined) {
      const bodyHtml = renderJsonTree(resp.body, 0, true);
      respHtml += makeCollapsible('Body', bodyHtml, false);
    }
    respContent.innerHTML = respHtml;

    // Bind collapsible toggles
    bindCollapsibles(reqContent);
    bindCollapsibles(respContent);
  }

  function renderHeaders(headers) {
    if (!headers || typeof headers !== 'object') return '<div style="color:var(--text-dim)">-</div>';
    let html = '<div class="json-tree">';
    for (const [k, v] of Object.entries(headers)) {
      html += '<div><span class="jt-key">' + escHtml(k) + '</span>: <span class="jt-str">' + escHtml(String(v)) + '</span></div>';
    }
    html += '</div>';
    return html;
  }

  // ---- Collapsible section ----
  function makeCollapsible(title, contentHtml, defaultCollapsed) {
    const sign = defaultCollapsed ? '+' : '-';
    const display = defaultCollapsed ? 'display:none' : '';
    return '<div class="collapsible-toggle" data-collapsed="' + defaultCollapsed + '"><span class="sign">' + sign + '</span> ' + escHtml(title) + '</div>' +
           '<div class="collapsible-content" style="' + display + '">' + contentHtml + '</div>';
  }

  function bindCollapsibles(root) {
    root.querySelectorAll('.collapsible-toggle').forEach(toggle => {
      toggle.addEventListener('click', () => {
        const content = toggle.nextElementSibling;
        const collapsed = toggle.dataset.collapsed === 'true';
        if (collapsed) {
          content.style.display = '';
          toggle.querySelector('.sign').textContent = '-';
          toggle.dataset.collapsed = 'false';
        } else {
          content.style.display = 'none';
          toggle.querySelector('.sign').textContent = '+';
          toggle.dataset.collapsed = 'true';
        }
      });
    });
  }

  // ---- JSON Tree Renderer ----
  function renderJsonTree(data, depth, isMessagesBody) {
    if (data === null) return '<span class="jt-null">null</span>';
    if (data === undefined) return '<span class="jt-null">undefined</span>';

    const type = typeof data;

    if (type === 'string') {
      return renderString(data);
    }
    if (type === 'number') {
      return '<span class="jt-num">' + data + '</span>';
    }
    if (type === 'boolean') {
      return '<span class="jt-bool">' + data + '</span>';
    }

    if (Array.isArray(data)) {
      // Special handling for messages arrays
      if (isMessagesBody && depth === 0) {
        return renderMessagesArray(data);
      }
      return renderArray(data, depth, isMessagesBody);
    }

    if (type === 'object') {
      return renderObject(data, depth, isMessagesBody);
    }

    return escHtml(String(data));
  }

  function renderString(s) {
    if (s.length > 200) {
      const id = 'ls_' + Math.random().toString(36).slice(2, 10);
      const preview = escHtml(s.slice(0, 200));
      const full = escHtml(s);
      return '<span id="' + id + '_short" class="jt-str">"' + preview + '..."' +
             '<span class="long-str-toggle" onclick="document.getElementById(\'' + id + '_short\').style.display=\'none\';document.getElementById(\'' + id + '_full\').style.display=\'inline\'">click to expand</span></span>' +
             '<span id="' + id + '_full" class="jt-str" style="display:none">"' + full + '"' +
             '<span class="long-str-toggle" onclick="document.getElementById(\'' + id + '_full\').style.display=\'none\';document.getElementById(\'' + id + '_short\').style.display=\'inline\'">collapse</span></span>';
    }
    return '<span class="jt-str">"' + escHtml(s) + '"</span>';
  }

  function renderObject(obj, depth, isMessagesBody) {
    const keys = Object.keys(obj);
    if (keys.length === 0) return '<span class="jt-bracket">{}</span>';

    const id = 'obj_' + Math.random().toString(36).slice(2, 10);
    let html = '<span class="jt-toggle" data-target="' + id + '">- </span>';
    html += '<span class="jt-bracket">{</span>';
    html += '<div class="jt-children" id="' + id + '">';
    keys.forEach((k, i) => {
      // Detect if this is a messages field at depth 0
      const isMsgField = (k === 'messages' && Array.isArray(obj[k]) && depth === 0);
      html += '<div><span class="jt-key">' + escHtml(k) + '</span>: ' +
              renderJsonTree(obj[k], depth + 1, isMsgField) +
              (i < keys.length - 1 ? '<span class="jt-bracket">,</span>' : '') +
              '</div>';
    });
    html += '</div>';
    html += '<span class="jt-bracket">}</span>';
    return html;
  }

  function renderArray(arr, depth, isMessagesBody) {
    if (arr.length === 0) return '<span class="jt-bracket">[]</span>';

    const id = 'arr_' + Math.random().toString(36).slice(2, 10);
    let html = '<span class="jt-toggle" data-target="' + id + '">- </span>';
    html += '<span class="jt-bracket">[</span> <span class="jt-collapsed-hint" id="' + id + '_hint" style="display:none">' + arr.length + ' items</span>';
    html += '<div class="jt-children" id="' + id + '">';
    arr.forEach((item, i) => {
      html += '<div>' + renderJsonTree(item, depth + 1, isMessagesBody) +
              (i < arr.length - 1 ? '<span class="jt-bracket">,</span>' : '') +
              '</div>';
    });
    html += '</div>';
    html += '<span class="jt-bracket">]</span>';
    return html;
  }

  function renderMessagesArray(messages) {
    if (messages.length === 0) return '<span class="jt-bracket">[]</span>';

    const id = 'msgs_' + Math.random().toString(36).slice(2, 10);
    let html = '<span class="jt-toggle" data-target="' + id + '">- </span>';
    html += '<span class="jt-bracket">[</span> <span class="jt-collapsed-hint" id="' + id + '_hint" style="display:none">' + messages.length + ' messages</span>';
    html += '<div class="jt-children" id="' + id + '">';

    messages.forEach((msg, i) => {
      const role = (msg && msg.role) || 'unknown';
      const roleClass = 'msg-role-' + role;
      html += '<div class="msg-block ' + roleClass + '">';
      html += '<div style="font-weight:600;font-size:11px;margin-bottom:4px">' + escHtml(role.toUpperCase()) + '</div>';
      // Render content separately
      if (msg && msg.content !== undefined) {
        html += renderJsonTree(msg.content, 1, false);
      } else {
        html += renderJsonTree(msg, 1, false);
      }
      html += '</div>';
    });

    html += '</div>';
    html += '<span class="jt-bracket">]</span>';
    return html;
  }

  // ---- Tree toggle binding (delegated) ----
  document.addEventListener('click', (e) => {
    const toggle = e.target.closest('.jt-toggle');
    if (!toggle) return;
    const targetId = toggle.dataset.target;
    const target = document.getElementById(targetId);
    const hint = document.getElementById(targetId + '_hint');
    if (!target) return;
    const hidden = target.style.display === 'none';
    if (hidden) {
      target.style.display = '';
      toggle.textContent = '- ';
      if (hint) hint.style.display = 'none';
    } else {
      target.style.display = 'none';
      toggle.textContent = '+ ';
      if (hint) hint.style.display = 'inline';
    }
  });

  // ---- Utility ----
  function escHtml(s) {
    if (typeof s !== 'string') s = String(s);
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
})();
</script>
</body>
</html>
"""
