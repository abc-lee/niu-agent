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
  --navbar-h: 48px;
  --stats-h: 40px;
}

html, body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  height: 100vh;
  overflow: hidden;
}

/* ---- Top Nav ---- */
.navbar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 24px;
  background: var(--card);
  border-bottom: 1px solid var(--border);
  height: var(--navbar-h);
  flex-shrink: 0;
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
  padding: 8px 24px;
  background: var(--card);
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  color: var(--text-dim);
  flex-wrap: wrap;
  height: var(--stats-h);
  align-items: center;
  flex-shrink: 0;
}
.stats-bar .stat-val {
  color: var(--text);
  font-weight: 600;
  margin-left: 4px;
}

/* ---- Main Layout: Left-Right Split ---- */
.main-container {
  display: flex;
  height: calc(100vh - var(--navbar-h) - var(--stats-h));
  overflow: hidden;
}

/* ---- Left Panel: List ---- */
.list-panel {
  width: 40%;
  min-width: 320px;
  overflow-y: auto;
  border-right: 1px solid var(--border);
  flex-shrink: 0;
}
.table-wrap {
  padding: 0;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
thead {
  position: sticky;
  top: 0;
  z-index: 10;
  background: var(--card);
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
tbody tr.active { background: var(--accent-dim); color: #fff; }
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

/* ---- Right Panel: Detail ---- */
.detail-panel {
  width: 60%;
  overflow-y: auto;
  background: var(--bg);
  display: flex;
  flex-direction: column;
}
.detail-panel .detail-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-dim);
  font-size: 15px;
}
.detail-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--card);
  font-size: 14px;
  font-weight: 600;
  flex-shrink: 0;
}
.detail-header button {
  background: none;
  border: none;
  color: var(--text-dim);
  cursor: pointer;
  font-size: 18px;
}

/* ---- Tabs ---- */
.detail-tabs {
  display: flex;
  border-bottom: 1px solid var(--border);
  background: var(--card);
  flex-shrink: 0;
}
.detail-tab {
  padding: 8px 20px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-dim);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: color .15s, border-color .15s;
  user-select: none;
}
.detail-tab:hover { color: var(--text); }
.detail-tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}

.detail-body {
  flex: 1;
  overflow-y: auto;
}
.detail-content {
  padding: 12px 16px;
  display: none;
}
.detail-content.active { display: block; }
.detail-content h3 {
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
.detail-meta {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 10px;
  font-size: 12px;
}
.detail-meta-item {
  color: var(--text-dim);
}
.detail-meta-item span {
  color: var(--text);
  font-weight: 600;
}
.usage-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  margin-bottom: 10px;
}
.usage-cell {
  background: var(--card);
  border-radius: var(--radius);
  padding: 8px 12px;
  text-align: center;
}
.usage-cell .usage-label {
  font-size: 11px;
  color: var(--text-dim);
  text-transform: uppercase;
}
.usage-cell .usage-value {
  font-size: 16px;
  font-weight: 700;
  color: var(--text);
  margin-top: 2px;
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

/* Formatted text (preserves newlines) */
.formatted-text {
  white-space: pre-wrap;
  word-break: break-word;
  font-family: "SF Mono", "Fira Code", "Consolas", monospace;
  font-size: 12px;
  line-height: 1.5;
}

/* ---- Empty state ---- */
.empty-state {
  text-align: center;
  padding: 60px 24px;
  color: var(--text-dim);
  font-size: 15px;
}

/* ---- Responsive ---- */
@media (max-width: 768px) {
  .main-container { flex-direction: column; }
  .list-panel { width: 100%; min-width: 0; max-height: 40vh; border-right: none; border-bottom: 1px solid var(--border); }
  .detail-panel { width: 100%; }
  .stats-bar { gap: 12px; }
  .usage-grid { grid-template-columns: 1fr; }
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

<div class="main-container">
  <!-- Left Panel: Entry List -->
  <div class="list-panel" id="listPanel">
    <div class="table-wrap" id="tableWrap">
      <div class="empty-state" id="emptyState">Select a date to view logs</div>
      <table id="logTable" style="display:none">
        <thead>
          <tr>
            <th>#</th>
            <th>Time</th>
            <th>Model</th>
            <th>Prompt</th>
            <th>Completion</th>
          </tr>
        </thead>
        <tbody id="logBody"></tbody>
      </table>
    </div>
  </div>

  <!-- Right Panel: Detail -->
  <div class="detail-panel" id="detailPanel">
    <div class="detail-empty" id="detailEmpty">Select an entry to view details</div>
    <div id="detailContent" style="display:none">
      <div class="detail-header">
        <span id="detailTitle">Entry Detail</span>
        <button id="detailClose">&times;</button>
      </div>
      <div class="detail-tabs" id="detailTabs">
        <div class="detail-tab" data-tab="transport">Transport</div>
        <div class="detail-tab" data-tab="request">Request</div>
        <div class="detail-tab" data-tab="response">Response</div>
      </div>
      <div class="detail-body">
        <div class="detail-content" id="contentTransport"></div>
        <div class="detail-content" id="contentRequest"></div>
        <div class="detail-content" id="contentResponse"></div>
      </div>
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
  const detailEmpty = document.getElementById('detailEmpty');
  const detailContent = document.getElementById('detailContent');
  const detailTitle = document.getElementById('detailTitle');
  const detailClose = document.getElementById('detailClose');
  const contentTransport = document.getElementById('contentTransport');
  const contentRequest = document.getElementById('contentRequest');
  const contentResponse = document.getElementById('contentResponse');
  const detailTabs = document.getElementById('detailTabs');

  let currentDate = '';
  let currentSeq = null;
  let entriesCache = [];
  let currentDetailData = null;

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
    currentSeq = null;
    document.querySelectorAll('#logBody tr.active').forEach(r => r.classList.remove('active'));
    detailContent.style.display = 'none';
    detailEmpty.style.display = 'flex';
  });

  // ---- Tab switching ----
  detailTabs.addEventListener('click', (e) => {
    const tab = e.target.closest('.detail-tab');
    if (!tab) return;
    const tabName = tab.dataset.tab;
    switchTab(tabName);
  });

  function switchTab(tabName) {
    detailTabs.querySelectorAll('.detail-tab').forEach(t => {
      t.classList.toggle('active', t.dataset.tab === tabName);
    });
    document.querySelectorAll('.detail-content').forEach(c => {
      c.classList.remove('active');
    });
    const target = document.getElementById('content' + tabName.charAt(0).toUpperCase() + tabName.slice(1));
    if (target) target.classList.add('active');
  }

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
    currentSeq = null;
    detailContent.style.display = 'none';
    detailEmpty.style.display = 'flex';
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
      const streamTag = e.streaming ? '<span class="tag-stream">Stream</span>' : '';
      const prompt = e.prompt_tokens != null ? e.prompt_tokens.toLocaleString() : '-';
      const completion = e.completion_tokens != null ? e.completion_tokens.toLocaleString() : '-';

      tr.innerHTML =
        '<td>' + e.seq + streamTag + '</td>' +
        '<td>' + time + '</td>' +
        '<td>' + escHtml(model) + '</td>' +
        '<td>' + prompt + '</td>' +
        '<td>' + completion + '</td>';

      tr.addEventListener('click', () => selectEntry(e.seq, tr));
      logBody.appendChild(tr);
    });
  }

  // ---- Select entry (always shows detail, no toggle-off) ----
  async function selectEntry(seq, tr) {
    document.querySelectorAll('#logBody tr.active').forEach(r => r.classList.remove('active'));
    tr.classList.add('active');
    currentSeq = seq;
    detailTitle.textContent = 'Entry #' + seq;
    contentTransport.innerHTML = '<div style="color:var(--text-dim)">Loading...</div>';
    contentRequest.innerHTML = '<div style="color:var(--text-dim)">Loading...</div>';
    contentResponse.innerHTML = '<div style="color:var(--text-dim)">Loading...</div>';
    detailEmpty.style.display = 'none';
    detailContent.style.display = 'flex';
    detailContent.style.flexDirection = 'column';
    detailContent.style.flex = '1';
    detailContent.style.overflow = 'hidden';

    try {
      const res = await fetch('/http-log/' + currentDate + '/entries/' + seq);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      currentDetailData = data;
      renderDetail(data);
    } catch(e) {
      contentTransport.innerHTML = '<div style="color:var(--error)">Failed to load: ' + escHtml(e.message) + '</div>';
    }
  }

  // ---- Render detail ----
  function renderDetail(data) {
    const transport = data.transport || {};
    const request = data.request || {};
    const response = data.response || {};

    // ---- Transport Tab ----
    let transHtml = '';
    const tReq = transport.request || {};
    const tResp = transport.response || {};

    // URL + Method
    if (tReq.method || tReq.url) {
      transHtml += '<div class="detail-url">' + escHtml(tReq.method || '') + ' ' + escHtml(tReq.url || '') + '</div>';
    }
    // Elapsed
    if (transport.elapsed_ms != null) {
      transHtml += '<div class="detail-meta"><div class="detail-meta-item">Elapsed: <span>' + transport.elapsed_ms + ' ms</span></div></div>';
    }
    // Request Headers
    transHtml += makeCollapsible('Request Headers', renderHeaders(tReq.headers), true);
    // Request Body
    if (tReq.body !== undefined) {
      transHtml += makeCollapsible('Request Body', renderJsonTree(tReq.body, 0, false), true);
    }
    // Response Status
    if (tResp.status_code != null) {
      const cls = tResp.status_code < 400 ? 'status-ok' : 'status-err';
      transHtml += '<div style="margin:8px 0">Response Status: <span class="' + cls + '">' + tResp.status_code + '</span></div>';
    }
    // Response Headers
    transHtml += makeCollapsible('Response Headers', renderHeaders(tResp.headers), true);
    // Response Body
    if (tResp.body !== undefined) {
      const body = tResp.body;
      if (typeof body === 'object' && body !== null && body.streaming === true) {
        transHtml += makeCollapsible('Response Body', '<div style="color:var(--orange);font-style:italic">Streaming response -- content captured in application layer</div>', false);
      } else {
        transHtml += makeCollapsible('Response Body', renderJsonTree(body, 0, false), true);
      }
    }
    if (!transHtml) {
      transHtml = '<div style="color:var(--text-dim);padding:20px;text-align:center">No transport data available</div>';
    }
    contentTransport.innerHTML = transHtml;

    // ---- Request Tab ----
    let reqHtml = '';
    // Model + Provider + Timestamp
    const reqMeta = [];
    if (request.model) reqMeta.push('Model: <span>' + escHtml(request.model) + '</span>');
    if (request.provider) reqMeta.push('Provider: <span>' + escHtml(request.provider) + '</span>');
    if (request.timestamp) reqMeta.push('Time: <span>' + escHtml(request.timestamp) + '</span>');
    if (reqMeta.length > 0) {
      reqHtml += '<div class="detail-meta">' + reqMeta.map(m => '<div class="detail-meta-item">' + m + '</div>').join('') + '</div>';
    }
    // Messages
    if (request.messages) {
      reqHtml += makeCollapsible('Messages (' + request.messages.length + ')', renderMessagesArray(request.messages), false);
    }
    // Tools
    if (request.tools) {
      reqHtml += makeCollapsible('Tools (' + request.tools.length + ')', renderJsonTree(request.tools, 0, false), true);
    }
    // Provider Params
    if (request.provider_params) {
      reqHtml += makeCollapsible('Provider Params', renderJsonTree(request.provider_params, 0, false), true);
    }
    // Request Params
    if (request.request_params) {
      reqHtml += makeCollapsible('Request Params', renderJsonTree(request.request_params, 0, false), true);
    }
    if (!reqHtml) {
      reqHtml = '<div style="color:var(--text-dim);padding:20px;text-align:center">No application request data available</div>';
    }
    contentRequest.innerHTML = reqHtml;

    // ---- Response Tab ----
    let respHtml = '';
    // Model + Timestamp
    const respMeta = [];
    if (response.model) respMeta.push('Model: <span>' + escHtml(response.model) + '</span>');
    if (response.timestamp) respMeta.push('Time: <span>' + escHtml(response.timestamp) + '</span>');
    if (respMeta.length > 0) {
      respHtml += '<div class="detail-meta">' + respMeta.map(m => '<div class="detail-meta-item">' + m + '</div>').join('') + '</div>';
    }
    // Usage
    const usage = response.usage;
    if (usage && typeof usage === 'object') {
      respHtml += '<div class="usage-grid">';
      respHtml += '<div class="usage-cell"><div class="usage-label">Prompt</div><div class="usage-value">' + (usage.prompt_tokens != null ? usage.prompt_tokens.toLocaleString() : '-') + '</div></div>';
      respHtml += '<div class="usage-cell"><div class="usage-label">Completion</div><div class="usage-value">' + (usage.completion_tokens != null ? usage.completion_tokens.toLocaleString() : '-') + '</div></div>';
      respHtml += '<div class="usage-cell"><div class="usage-label">Total</div><div class="usage-value">' + (usage.total_tokens != null ? usage.total_tokens.toLocaleString() : '-') + '</div></div>';
      respHtml += '</div>';
    }
    // Content
    if (response.content !== undefined && response.content !== null) {
      respHtml += makeCollapsible('Content', renderLongText(response.content), false);
    }
    // Thinking
    if (response.thinking !== undefined && response.thinking !== null) {
      respHtml += makeCollapsible('Thinking', renderLongText(response.thinking), true);
    }
    // Tool Calls
    if (response.tool_calls) {
      respHtml += makeCollapsible('Tool Calls (' + response.tool_calls.length + ')', renderJsonTree(response.tool_calls, 0, false), false);
    }
    if (!respHtml) {
      respHtml = '<div style="color:var(--text-dim);padding:20px;text-align:center">No application response data available</div>';
    }
    contentResponse.innerHTML = respHtml;

    // Bind collapsibles
    bindCollapsibles(contentTransport);
    bindCollapsibles(contentRequest);
    bindCollapsibles(contentResponse);

    // Select default tab: Request if available, else Transport
    if (data.request) {
      switchTab('request');
    } else {
      switchTab('transport');
    }
  }

  // ---- Render long text with expand/collapse ----
  function renderLongText(text) {
    if (typeof text !== 'string') {
      return renderJsonTree(text, 0, false);
    }
    const id = 'lt_' + Math.random().toString(36).slice(2, 10);
    const escaped = escHtml(text);
    // Always show full text, but allow collapsing for very long strings
    if (text.length > 500) {
      return '<div id="' + id + '">' +
             '<div id="' + id + '_short" class="formatted-text" style="max-height:200px;overflow:hidden;position:relative">' +
             escaped +
             '<div style="position:absolute;bottom:0;left:0;right:0;height:40px;background:linear-gradient(transparent,var(--bg));cursor:pointer" onclick="document.getElementById(\'' + id + '_short\').style.display=\'none\';document.getElementById(\'' + id + '_full\').style.display=\'block\'"></div>' +
             '</div>' +
             '<div id="' + id + '_full" class="formatted-text" style="display:none">' +
             escaped +
             '<span class="long-str-toggle" onclick="document.getElementById(\'' + id + '_full\').style.display=\'none\';document.getElementById(\'' + id + '_short\').style.display=\'block\'">collapse</span>' +
             '</div></div>';
    }
    return '<div class="formatted-text">' + escaped + '</div>';
  }

  // ---- Render formatted text for message content ----
  function renderFormattedText(text) {
    if (typeof text !== 'string') return renderJsonTree(text, 1, false);
    const id = 'ft_' + Math.random().toString(36).slice(2, 10);
    const escaped = escHtml(text);
    if (text.length > 500) {
      return '<div id="' + id + '">' +
             '<div id="' + id + '_short" class="formatted-text" style="max-height:150px;overflow:hidden;position:relative">' +
             escaped +
             '<div style="position:absolute;bottom:0;left:0;right:0;height:40px;background:linear-gradient(transparent,var(--card));cursor:pointer" onclick="document.getElementById(\'' + id + '_short\').style.display=\'none\';document.getElementById(\'' + id + '_full\').style.display=\'block\'"></div>' +
             '</div>' +
             '<div id="' + id + '_full" class="formatted-text" style="display:none">' +
             escaped +
             '<span class="long-str-toggle" onclick="document.getElementById(\'' + id + '_full\').style.display=\'none\';document.getElementById(\'' + id + '_short\').style.display=\'block\'">collapse</span>' +
             '</div></div>';
    }
    return '<div class="formatted-text">' + escaped + '</div>';
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
    // If string contains newlines, render with pre-wrap to preserve them
    if (s.indexOf('\n') !== -1) {
      const id = 'ns_' + Math.random().toString(36).slice(2, 10);
      const escaped = escHtml(s);
      if (s.length > 200) {
        const preview = escHtml(s.slice(0, 200));
        return '<span id="' + id + '_short" class="jt-str">"' + preview + '..."' +
               '<span class="long-str-toggle" onclick="document.getElementById(\'' + id + '_short\').style.display=\'none\';document.getElementById(\'' + id + '_full\').style.display=\'inline\'">expand</span></span>' +
               '<span id="' + id + '_full" class="jt-str" style="display:none">"<pre class="formatted-text" style="display:inline;margin:0;padding:0;background:none;color:inherit;font-size:inherit">' + escaped + '</pre>"' +
               '<span class="long-str-toggle" onclick="document.getElementById(\'' + id + '_full\').style.display=\'none\';document.getElementById(\'' + id + '_short\').style.display=\'inline\'">collapse</span></span>';
      }
      return '<span class="jt-str">"<pre class="formatted-text" style="display:inline;margin:0;padding:0;background:none;color:inherit;font-size:inherit">' + escaped + '</pre>"</span>';
    }
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
      // Render content with formatted text for long/multiline strings
      if (msg && msg.content !== undefined) {
        if (typeof msg.content === 'string' && (msg.content.length > 100 || msg.content.indexOf('\n') !== -1)) {
          html += renderFormattedText(msg.content);
        } else {
          html += renderJsonTree(msg.content, 1, false);
        }
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
