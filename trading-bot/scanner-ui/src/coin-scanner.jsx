import { useState, useRef } from "react";

const API_BASE = "https://refinance-agreeable-purchase.ngrok-free.dev";
const WS_URL   = "wss://refinance-agreeable-purchase.ngrok-free.app/ws/scan";

const NGROK_HEADERS = { "ngrok-skip-browser-warning": "true" };
const SIGNAL_COLOR  = { BUY: "#22c55e", SELL: "#ef4444", HOLD: "#94a3b8" };

function SignalBadge({ signal }) {
  return (
    <span style={{
      background: SIGNAL_COLOR[signal] ?? "#94a3b8",
      color: "#fff", borderRadius: 4, padding: "2px 8px",
      fontSize: 12, fontWeight: 700, letterSpacing: 1,
    }}>
      {signal}
    </span>
  );
}

function ConfidenceBar({ value }) {
  const pct = Math.round(value * 100);
  const color = pct >= 80 ? "#22c55e" : pct >= 65 ? "#f59e0b" : "#94a3b8";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <div style={{ width: 80, height: 6, background: "#1e293b", borderRadius: 3, overflow: "hidden" }}>
        <div style={{ width: `${pct}%`, height: "100%", background: color, borderRadius: 3 }} />
      </div>
      <span style={{ fontSize: 12, color }}>{pct}%</span>
    </div>
  );
}

function SignalRow({ r, highlight }) {
  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: "1fr 80px 120px auto",
      alignItems: "center",
      gap: 12, padding: "10px 14px", borderRadius: 6,
      background: highlight ? "rgba(234,179,8,0.08)" : "rgba(255,255,255,0.03)",
      border: highlight ? "1px solid rgba(234,179,8,0.3)" : "1px solid transparent",
      marginBottom: 4,
    }}>
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontWeight: 600, fontSize: 14 }}>{r.symbol}</span>
          {highlight && (
            <span style={{ fontSize: 10, background: "#ca8a04", color: "#fff", borderRadius: 3, padding: "1px 5px", fontWeight: 700, letterSpacing: 0.5 }}>
              CROSS-CONFIRMED
            </span>
          )}
        </div>
        <div style={{ fontSize: 11, color: "#64748b", marginTop: 2 }}>
          {r.exchanges?.join(" + ")}
        </div>
      </div>
      <SignalBadge signal={r.signal} />
      <ConfidenceBar value={r.confidence} />
      <div style={{ fontSize: 12, color: "#94a3b8", textAlign: "right" }}>
        ${Number(r.price).toLocaleString(undefined, { maximumFractionDigits: 4 })}
      </div>
    </div>
  );
}

// ── Cached results panel (from /bot/last-scan) ────────────────────────────────

function BotSignalsPanel({ data, onClose }) {
  const [showAll, setShowAll] = useState(false);
  const crossConfirmed = data.cross_confirmed ?? [];
  const singleExchange = data.single_exchange ?? [];
  const visibleSingle  = showAll ? singleExchange : singleExchange.slice(0, 10);

  return (
    <div style={{ background: "#0f172a", border: "1px solid #1e293b", borderRadius: 10, padding: 20, marginTop: 16, color: "#e2e8f0", fontFamily: "monospace" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 16 }}>
        <div>
          <div style={{ fontSize: 16, fontWeight: 700 }}>Bot Scan Results</div>
          <div style={{ fontSize: 12, color: "#64748b", marginTop: 2 }}>
            {data.scanned_at ? `Scanned at ${new Date(data.scanned_at).toLocaleString()}` : "No timestamp"}
            {" · "}{data.total_scanned ?? 0} coins · {data.executed_count ?? 0} orders
          </div>
        </div>
        <button onClick={onClose} style={{ background: "none", border: "none", color: "#64748b", cursor: "pointer", fontSize: 18 }}>✕</button>
      </div>

      {crossConfirmed.length > 0 && (
        <div style={{ marginBottom: 20 }}>
          <div style={{ fontSize: 12, fontWeight: 700, color: "#ca8a04", textTransform: "uppercase", letterSpacing: 1, marginBottom: 8 }}>
            ★ Cross-Confirmed ({crossConfirmed.length})
          </div>
          {crossConfirmed.map(r => <SignalRow key={r.symbol} r={r} highlight />)}
        </div>
      )}

      {singleExchange.length > 0 && (
        <div>
          <div style={{ fontSize: 12, fontWeight: 700, color: "#64748b", textTransform: "uppercase", letterSpacing: 1, marginBottom: 8 }}>
            Single Exchange ({singleExchange.length})
          </div>
          {visibleSingle.map(r => <SignalRow key={r.symbol} r={r} highlight={false} />)}
          {singleExchange.length > 10 && (
            <button onClick={() => setShowAll(v => !v)} style={{ background: "none", border: "1px solid #334155", color: "#94a3b8", borderRadius: 4, padding: "6px 14px", cursor: "pointer", fontSize: 12, marginTop: 8 }}>
              {showAll ? "Show less" : `Show ${singleExchange.length - 10} more`}
            </button>
          )}
        </div>
      )}

      {crossConfirmed.length === 0 && singleExchange.length === 0 && (
        <div style={{ color: "#64748b", textAlign: "center", padding: "24px 0" }}>No actionable signals in the last scan.</div>
      )}
    </div>
  );
}

// ── Live scan panel (from WebSocket /ws/scan) ─────────────────────────────────

function LiveScanPanel({ results, progress, total, elapsed, done, onClose }) {
  const [showAll, setShowAll] = useState(false);
  const signals   = results.filter(r => r.signal !== "HOLD" && r.confidence >= 0.55);
  const visible   = showAll ? signals : signals.slice(0, 15);
  const pct       = total > 0 ? Math.round((progress / total) * 100) : 0;

  return (
    <div style={{ background: "#0f172a", border: "1px solid #1e293b", borderRadius: 10, padding: 20, marginTop: 16, color: "#e2e8f0", fontFamily: "monospace" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 12 }}>
        <div>
          <div style={{ fontSize: 16, fontWeight: 700 }}>Live Scan</div>
          <div style={{ fontSize: 12, color: "#64748b", marginTop: 2 }}>
            {done
              ? `Complete — ${progress} coins in ${elapsed}s · ${signals.length} signals`
              : `${progress} / ${total} coins (${pct}%)`}
          </div>
        </div>
        <button onClick={onClose} style={{ background: "none", border: "none", color: "#64748b", cursor: "pointer", fontSize: 18 }}>✕</button>
      </div>

      {/* Progress bar */}
      {!done && (
        <div style={{ width: "100%", height: 4, background: "#1e293b", borderRadius: 2, marginBottom: 16, overflow: "hidden" }}>
          <div style={{ width: `${pct}%`, height: "100%", background: "#6366f1", borderRadius: 2, transition: "width 0.3s" }} />
        </div>
      )}

      {signals.length > 0 && (
        <>
          <div style={{ fontSize: 12, fontWeight: 700, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 1, marginBottom: 8 }}>
            Signals ({signals.length})
          </div>
          {visible.map(r => <SignalRow key={r.symbol} r={r} highlight={false} />)}
          {signals.length > 15 && (
            <button onClick={() => setShowAll(v => !v)} style={{ background: "none", border: "1px solid #334155", color: "#94a3b8", borderRadius: 4, padding: "6px 14px", cursor: "pointer", fontSize: 12, marginTop: 8 }}>
              {showAll ? "Show less" : `Show ${signals.length - 15} more`}
            </button>
          )}
        </>
      )}

      {!done && signals.length === 0 && (
        <div style={{ color: "#475569", textAlign: "center", padding: "24px 0", fontSize: 13 }}>Scanning…</div>
      )}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function CoinScanner() {
  // Cached scan state
  const [scanData, setScanData]   = useState(null);
  const [loading, setLoading]     = useState(false);
  const [fetchError, setFetchError] = useState(null);

  // Live WebSocket scan state
  const [wsResults, setWsResults]   = useState([]);
  const [wsProgress, setWsProgress] = useState(0);
  const [wsTotal, setWsTotal]       = useState(0);
  const [wsElapsed, setWsElapsed]   = useState(0);
  const [wsDone, setWsDone]         = useState(false);
  const [wsActive, setWsActive]     = useState(false);
  const [wsError, setWsError]       = useState(null);
  const wsRef = useRef(null);

  // ── Fetch cached results ──────────────────────────────────────────────────

  async function fetchBotSignals() {
    setLoading(true);
    setFetchError(null);
    try {
      const res = await fetch(`${API_BASE}/bot/last-scan`, { headers: NGROK_HEADERS });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      if (!json.available) {
        setFetchError(json.message ?? "No scan data available yet.");
        setScanData(null);
      } else {
        setScanData(json);
      }
    } catch (err) {
      setFetchError(err.message);
      setScanData(null);
    } finally {
      setLoading(false);
    }
  }

  // ── WebSocket live scan ───────────────────────────────────────────────────

  function startLiveScan(limit = 100) {
    stopLiveScan();
    setWsResults([]);
    setWsProgress(0);
    setWsTotal(0);
    setWsElapsed(0);
    setWsDone(false);
    setWsError(null);
    setWsActive(true);

    const ws = new WebSocket(`${WS_URL}?ngrok-skip-browser-warning=true`);
    wsRef.current = ws;

    ws.onopen = () => {
      ws.send(JSON.stringify({ action: "start", direction: "ALL", limit }));
    };

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);

      if (msg.type === "start") {
        setWsTotal(msg.total);
      } else if (msg.type === "result") {
        setWsProgress(msg.done);
        if (msg.signal !== "HOLD" && msg.confidence >= 0.55) {
          setWsResults(prev => {
            const next = [...prev, msg];
            next.sort((a, b) => b.confidence - a.confidence);
            return next;
          });
        }
      } else if (msg.type === "complete") {
        setWsProgress(msg.total_scanned);
        setWsElapsed(msg.elapsed_seconds);
        setWsDone(true);
        setWsActive(false);
      } else if (msg.type === "error") {
        setWsError(msg.message);
        setWsActive(false);
      }
    };

    ws.onerror = () => {
      setWsError("WebSocket connection failed. Is the server running?");
      setWsActive(false);
    };

    ws.onclose = () => {
      setWsActive(false);
    };
  }

  function stopLiveScan() {
    if (wsRef.current) {
      try { wsRef.current.send(JSON.stringify({ action: "stop" })); } catch {}
      wsRef.current.close();
      wsRef.current = null;
    }
    setWsActive(false);
  }

  const showLivePanel = wsActive || wsDone || wsResults.length > 0;

  return (
    <div style={{ maxWidth: 860, margin: "0 auto", padding: 24, fontFamily: "monospace" }}>

      {/* Controls */}
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>

        {/* Cached scan */}
        <button onClick={fetchBotSignals} disabled={loading} style={{
          background: loading ? "#1e293b" : "#0ea5e9",
          color: "#fff", border: "none", borderRadius: 6,
          padding: "10px 20px", fontWeight: 700, fontSize: 13,
          cursor: loading ? "not-allowed" : "pointer", letterSpacing: 1,
        }}>
          {loading ? "Loading…" : "BOT SIGNALS"}
        </button>

        {/* Live scan */}
        {!wsActive ? (
          <button onClick={() => startLiveScan(100)} style={{
            background: "#6366f1", color: "#fff", border: "none", borderRadius: 6,
            padding: "10px 20px", fontWeight: 700, fontSize: 13, cursor: "pointer", letterSpacing: 1,
          }}>
            LIVE SCAN
          </button>
        ) : (
          <button onClick={stopLiveScan} style={{
            background: "rgba(239,68,68,0.15)", color: "#fca5a5",
            border: "1px solid rgba(239,68,68,0.3)", borderRadius: 6,
            padding: "10px 20px", fontWeight: 700, fontSize: 13, cursor: "pointer",
          }}>
            Stop
          </button>
        )}

        {scanData && !loading && (
          <span style={{ fontSize: 12, color: "#64748b" }}>
            {scanData.cross_confirmed?.length ?? 0} cross-confirmed · {scanData.single_exchange?.length ?? 0} single-exchange
          </span>
        )}
      </div>

      {/* Errors */}
      {fetchError && (
        <div style={{ marginTop: 12, padding: "10px 14px", background: "rgba(239,68,68,0.1)", border: "1px solid rgba(239,68,68,0.3)", borderRadius: 6, color: "#fca5a5", fontSize: 13 }}>
          {fetchError}
        </div>
      )}
      {wsError && (
        <div style={{ marginTop: 12, padding: "10px 14px", background: "rgba(239,68,68,0.1)", border: "1px solid rgba(239,68,68,0.3)", borderRadius: 6, color: "#fca5a5", fontSize: 13 }}>
          {wsError}
        </div>
      )}

      {/* Panels */}
      {scanData && (
        <BotSignalsPanel data={scanData} onClose={() => setScanData(null)} />
      )}

      {showLivePanel && (
        <LiveScanPanel
          results={wsResults}
          progress={wsProgress}
          total={wsTotal}
          elapsed={wsElapsed}
          done={wsDone}
          onClose={() => { stopLiveScan(); setWsResults([]); setWsDone(false); }}
        />
      )}
    </div>
  );
}
