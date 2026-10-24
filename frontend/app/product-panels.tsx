"use client";

import { type FormEvent, useCallback, useEffect, useState } from "react";
import { authenticatedFetch } from "../lib/api";

type Underlying = { symbol: string; exchange_segment: string; instrument_type: string };
type ScannerCandidate = {
  included: boolean; underlying: string; expiry: string; strike: number; option_type: "CALL" | "PUT";
  current_premium: number | null; total_score: number | null; volume: number | null; open_interest: number | null;
  implied_volatility: number | null; data_timestamp?: string; data_quality_status: string;
  targets?: { multiplier: string; target_value: string | number }[];
  historical_statistics?: { available?: boolean; maximum_multiple?: number | string | null; source_days?: number; reason?: string; calendar_expiry_confirmed?: boolean };
  explanation?: { inclusion_reasons?: string[]; exclusion_reasons?: string[] };
};
type ScannerResult = { status: string; message?: string; data_quality_status?: string; data_timestamp?: string; opportunities: ScannerCandidate[]; candidates?: ScannerCandidate[] };
type TargetSession = { session_id: string; status: string; underlying?: string; instrument?: string; expiry?: string; strike?: string | number; option_type?: string; reference_premium?: string | number; reference_timestamp?: string; latest_observed_premium?: string | number | null; latest_observation_timestamp?: string | null; latest_observation_ingested_at?: string | null; current_multiple?: string | number | null; data_quality?: string; verification_status?: string; targets?: { multiplier: string | number; target_value: string | number; status: string; first_reached_at?: string | null }[]; message?: string };
type Confirmation = { confirmation_required: boolean; request_id: string; underlying: string; expiry: string; strike: string | number; option_type: string; reference_premium: string | number; reference_timestamp: string; ingested_at: string; targets: { multiplier: number; target_value: string | number }[]; message: string };
type OptionUnderlying = { symbol: string };
type WatchlistEntry = { id: string; name: string; description?: string | null; is_default: boolean; items: WatchlistItem[] };
type WatchlistItem = { id: string; underlying: string; expiry?: string | null; strike?: string | number | null; option_type?: string | null; display_name?: string; note?: string | null; quote: { status: string; last_price: number | null; provider_timestamp?: string | null; ingested_at?: string | null } };

const show = (value: number | string | null | undefined, digits = 2) => value == null || !Number.isFinite(Number(value)) ? "Unavailable" : Number(value).toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const time = (value?: string | null) => value ? new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Kolkata" }).format(new Date(value)) : "Unavailable";

export function ScannerPanel({ accessToken, apiBase, compact = false }: { accessToken: string; apiBase: string; compact?: boolean }) {
  const [underlyings, setUnderlyings] = useState<Underlying[]>([]);
  const [underlying, setUnderlying] = useState("");
  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiry, setExpiry] = useState("");
  const [result, setResult] = useState<ScannerResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void authenticatedFetch(`${apiBase}/options/underlyings`, { cache: "no-store", headers: { Authorization: `Bearer ${accessToken}` } })
      .then(async (response) => { if (!response.ok) throw new Error("Dhan-confirmed instruments are unavailable."); return response.json() as Promise<{ underlyings: Underlying[] }>; })
      .then((body) => { if (active) { const values = body.underlyings ?? []; setUnderlyings(values); setUnderlying((current) => current || values[0]?.symbol || ""); } })
      .catch((cause) => { if (active) setError(cause instanceof Error ? cause.message : "Instrument list unavailable."); });
    return () => { active = false; };
  }, [apiBase, accessToken]);

  useEffect(() => {
    if (!underlying) { setExpiries([]); setExpiry(""); setResult(null); return; }
    let active = true;
    setExpiry(""); setExpiries([]); setResult(null);
    void authenticatedFetch(`${apiBase}/options/${encodeURIComponent(underlying)}/expiries`, { cache: "no-store", headers: { Authorization: `Bearer ${accessToken}` } })
      .then(async (response) => { if (!response.ok) throw new Error("Provider-confirmed expiries are unavailable."); return response.json() as Promise<{ available: boolean; expiries: string[] }>; })
      .then((body) => { if (active) { const values = body.available ? body.expiries ?? [] : []; setExpiries(values); setExpiry(values[0] ?? ""); } })
      .catch((cause) => { if (active) setError(cause instanceof Error ? cause.message : "Expiry data unavailable."); });
    return () => { active = false; };
  }, [apiBase, accessToken, underlying]);

  const scan = useCallback(async () => {
    if (!underlying || !expiry) return;
    setLoading(true); setError("");
    try {
      const query = new URLSearchParams({ underlying, expiry });
      const response = await authenticatedFetch(`${apiBase}/scanner/opportunities?${query}`, { cache: "no-store", headers: { Authorization: `Bearer ${accessToken}` } });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? `Scanner request failed (${response.status}).`);
      setResult(body as ScannerResult);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Dhan scanner data is unavailable."); setResult(null); }
    finally { setLoading(false); }
  }, [apiBase, accessToken, underlying, expiry]);

  useEffect(() => { if (underlying && expiry) void scan(); }, [scan, underlying, expiry]);
  const rows = result?.opportunities ?? [];
  const call = rows.find((item) => item.option_type === "CALL");
  const put = rows.find((item) => item.option_type === "PUT");
  const historical = rows.find((item) => item.historical_statistics?.available)?.historical_statistics;
  return <section className={`workspace-panel scanner-panel ${compact ? "is-compact" : ""}`} aria-labelledby={compact ? undefined : "scanner-title"}>
    <div className="content-heading"><div><p className="eyebrow">REAL DHAN DATA · VALIDATED HISTORY</p><h2 id={compact ? undefined : "scanner-title"}>10X Scanner</h2><p className="panel-description">Observation ranking only. Targets are reference-premium multiples, not predictions.</p></div><span className={`status-tag ${result?.status === "OPPORTUNITIES FOUND" ? "tag-live" : ""}`}>{loading ? "LOADING" : result?.status ?? "DATA UNAVAILABLE"}</span></div>
    <div className="scanner-controls"><label>Underlying<select value={underlying} onChange={(event) => setUnderlying(event.target.value)} disabled={!underlyings.length}>{underlyings.length ? underlyings.map((item) => <option key={item.symbol} value={item.symbol}>{item.symbol} · {item.exchange_segment}</option>) : <option value="">No synchronized underlyings</option>}</select></label><label>Expiry<select value={expiry} onChange={(event) => setExpiry(event.target.value)} disabled={!expiries.length}>{expiries.length ? expiries.map((item) => <option key={item} value={item}>{item}</option>) : <option value="">Unavailable</option>}</select></label><button type="button" className="secondary-button" disabled={loading || !expiry} onClick={() => void scan()}>{loading ? "Scanning…" : "Refresh scan"}</button></div>
    {error && <div className="inline-error" role="alert">{error}</div>}
    {result && <div className="data-provenance" role="status"><span>{result.message ?? result.data_quality_status}</span><span>Observation received {time(result.data_timestamp)}</span><span>{result.data_quality_status ?? "DATA UNAVAILABLE"}</span></div>}
    {compact && <div className="dashboard-comparisons"><div className="comparison-box"><span>TOP QUALIFYING CALL</span>{call ? <><strong>₹{show(call.current_premium)} <small>· {show(call.strike, 0)} strike</small></strong><small>{call.expiry} · score {show(call.total_score, 1)}</small></> : <b>Unavailable</b>}</div><div className="comparison-box"><span>TOP QUALIFYING PUT</span>{put ? <><strong>₹{show(put.current_premium)} <small>· {show(put.strike, 0)} strike</small></strong><small>{put.expiry} · score {show(put.total_score, 1)}</small></> : <b>Unavailable</b>}</div><div className="comparison-box history-stat"><span>VALIDATED HISTORICAL MAX MULTIPLE</span>{historical ? <><strong>{show(historical.maximum_multiple, 2)}×</strong><small>{show(historical.source_days, 0)} source days · rolling history</small></> : <b>Unavailable</b>}</div></div>}
    {rows.length ? <div className="scanner-results">{rows.slice(0, compact ? 3 : 30).map((row) => <article className="scanner-result" key={`${row.expiry}:${row.strike}:${row.option_type}`}><div className="scanner-result-heading"><strong>{row.underlying} {show(row.strike, 0)} {row.option_type}</strong><span className="score-badge">Score {show(row.total_score, 1)}</span></div><div className="scanner-metrics"><span>Premium <b>₹{show(row.current_premium)}</b></span><span>Volume <b>{show(row.volume, 0)}</b></span><span>OI <b>{show(row.open_interest, 0)}</b></span><span>IV <b>{show(row.implied_volatility)}</b></span></div><div className="target-chips">{row.targets?.map((target) => <span key={target.multiplier}>{target.multiplier} <b>₹{show(target.target_value)}</b></span>) ?? <span>Targets unavailable</span>}</div><small className="candidate-note">{row.explanation?.inclusion_reasons?.[0] ?? row.data_quality_status}</small></article>)}</div> : !loading && <div className="empty-state-panel compact-empty"><span className="empty-state-mark">⌁</span><div><strong>{result?.status === "NO CLEAR OPPORTUNITY" ? "NO CLEAR OPPORTUNITY" : "Scanner data unavailable"}</strong><p>{result?.message ?? (error || "Select a synchronized Dhan underlying and expiry to scan real contracts.")}</p></div><span className="status-tag">{result?.data_quality_status ?? "DATA UNAVAILABLE"}</span></div>}
    {!compact && <p className="panel-footnote">Missing factors are unavailable, not zero. The backend documents score coverage and excludes stale or invalid observations. No trades are executed.</p>}
  </section>;
}

export function TargetTrackerPanel({ accessToken, apiBase, compact = false }: { accessToken: string; apiBase: string; compact?: boolean }) {
  const [sessions, setSessions] = useState<TargetSession[]>([]);
  const [underlyings, setUnderlyings] = useState<string[]>([]);
  const [expiries, setExpiries] = useState<string[]>([]);
  const [form, setForm] = useState({ underlying: "", expiry: "", strike: "", option_type: "CALL" });
  const [requestId, setRequestId] = useState("");
  const [preview, setPreview] = useState<Confirmation | null>(null);
  const [starting, setStarting] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    void authenticatedFetch(`${apiBase}/options/underlyings`, { cache: "no-store", headers: { Authorization: `Bearer ${accessToken}` } })
      .then(async (response) => { if (!response.ok) throw new Error("Dhan-confirmed options are unavailable."); return response.json() as Promise<{ underlyings: OptionUnderlying[] }>; })
      .then((body) => { if (active) { const symbols = (body.underlyings ?? []).map((item) => item.symbol); setUnderlyings(symbols); setForm((current) => ({ ...current, underlying: current.underlying || symbols[0] || "" })); } })
      .catch((cause) => { if (active) setError(cause instanceof Error ? cause.message : "Dhan options unavailable."); });
    return () => { active = false; };
  }, [apiBase, accessToken]);
  useEffect(() => {
    if (!form.underlying) { setExpiries([]); return; }
    let active = true;
    setExpiries([]); setForm((current) => ({ ...current, expiry: "" })); setPreview(null);
    void authenticatedFetch(`${apiBase}/options/${encodeURIComponent(form.underlying)}/expiries`, { cache: "no-store", headers: { Authorization: `Bearer ${accessToken}` } })
      .then(async (response) => { if (!response.ok) throw new Error("Dhan expiry information is unavailable."); return response.json() as Promise<{ available: boolean; expiries: string[] }>; })
      .then((body) => { if (active) { const values = body.available ? body.expiries ?? [] : []; setExpiries(values); setForm((current) => ({ ...current, expiry: values[0] ?? "" })); } })
      .catch((cause) => { if (active) setError(cause instanceof Error ? cause.message : "Expiry data unavailable."); });
    return () => { active = false; };
  }, [apiBase, accessToken, form.underlying]);
  const load = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const response = await authenticatedFetch(`${apiBase}/targets`, { cache: "no-store", headers: { Authorization: `Bearer ${accessToken}` } });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "Target sessions are unavailable.");
      setSessions(body.targets ?? []);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Target sessions unavailable."); setSessions([]); }
    finally { setLoading(false); }
  }, [apiBase, accessToken]);
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, [load]);

  const start = async (confirm = false) => {
    const currentRequestId = requestId || crypto.randomUUID();
    setRequestId(currentRequestId); setStarting(true); setError("");
    const payload: Record<string, string> = { request_id: currentRequestId, ...form };
    if (confirm && preview) {
      payload.confirmed_reference_premium = String(preview.reference_premium);
      payload.confirmed_reference_timestamp = preview.reference_timestamp;
    }
    try {
      const response = await authenticatedFetch(`${apiBase}/targets/start`, { method: "POST", cache: "no-store", headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "Target tracking could not be started.");
      if (body.confirmation_required) setPreview(body as Confirmation);
      else { setPreview(null); setRequestId(""); setForm((current) => ({ ...current, strike: "" })); await load(); }
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Dhan reference unavailable."); }
    finally { setStarting(false); }
  };

  const cancel = async (id: string) => {
    if (!window.confirm("Stop this target tracking session? This action cannot be undone.")) return;
    try {
      const response = await authenticatedFetch(`${apiBase}/targets/${encodeURIComponent(id)}/stop`, { method: "POST", headers: { Authorization: `Bearer ${accessToken}` } });
      if (!response.ok) throw new Error("Could not stop this tracking session.");
      await load();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Unable to stop tracking."); }
  };

  return <section className={`workspace-panel target-panel ${compact ? "is-compact" : ""}`}><div className="content-heading"><div><p className="eyebrow">PERSONAL TRACKING</p><h2>Target tracker</h2><p className="panel-description">Premium levels based on each session’s original reference observation.</p></div><span className="status-tag">{loading ? "LOADING" : `${sessions.length} SESSIONS`}</span></div>
    {!compact && <form className="tracking-start-form" onSubmit={(event) => { event.preventDefault(); void start(false); }}><label>Underlying<select required value={form.underlying} onChange={(event) => { setPreview(null); setForm((current) => ({ ...current, underlying: event.target.value })); }} disabled={!underlyings.length}>{underlyings.map((symbol) => <option key={symbol} value={symbol}>{symbol}</option>)}</select></label><label>Expiry<select required value={form.expiry} onChange={(event) => { setPreview(null); setForm((current) => ({ ...current, expiry: event.target.value })); }} disabled={!expiries.length}>{expiries.map((value) => <option key={value} value={value}>{value}</option>)}</select></label><label>Strike<input required type="number" min="0.00000001" step="any" value={form.strike} onChange={(event) => { setPreview(null); setForm((current) => ({ ...current, strike: event.target.value })); }} /></label><label>Option type<select value={form.option_type} onChange={(event) => { setPreview(null); setForm((current) => ({ ...current, option_type: event.target.value })); }}><option value="CALL">CALL</option><option value="PUT">PUT</option></select></label><button type="submit" className="secondary-button" disabled={starting || !form.expiry || !form.strike}>{starting ? "Checking Dhan…" : preview ? "Refresh quote" : "Get live reference"}</button></form>}
    {error && <div className="inline-error" role="alert">{error}</div>}
    {preview && !compact && <div className="reference-confirmation" role="alert"><div><p className="eyebrow">DHANHQ REFERENCE · CONFIRM BEFORE SAVING</p><strong>{preview.underlying} {show(preview.strike, 0)} {preview.option_type} · {preview.expiry}</strong><p>Observed premium <b>₹{show(preview.reference_premium)}</b></p><p>Exchange time {time(preview.reference_timestamp)} · Received {time(preview.ingested_at)}</p><small>Confirming saves this premium and timestamp as the immutable baseline. A changed quote requires a new confirmation.</small></div><div className="confirmation-targets">{preview.targets.map((target) => <span key={target.multiplier}>{target.multiplier}X <b>₹{show(target.target_value)}</b></span>)}</div><button type="button" className="confirm-button" disabled={starting} onClick={() => void start(true)}>{starting ? "Confirming…" : "Confirm reference & start"}</button></div>}
    {sessions.length ? <div className="target-session-list">{sessions.slice(0, compact ? 2 : 100).map((item) => <article className="target-session" key={item.session_id}><div className="target-session-heading"><div><strong>{item.underlying ?? item.instrument ?? "Instrument unavailable"} · {show(item.strike, 0)} {item.option_type ?? ""}</strong><small>Expiry {item.expiry ?? "Unavailable"} · Reference ₹{show(item.reference_premium)} at {time(item.reference_timestamp)}</small></div><span className={`status-tag ${item.data_quality === "LIVE" ? "tag-live" : ""}`}>{item.status === "active" ? item.data_quality ?? "DATA STALE" : item.status?.toUpperCase()}</span></div><div className="current-target-observation"><span>Latest premium <b>{item.latest_observed_premium == null ? "Unavailable" : `₹${show(item.latest_observed_premium)}`}</b></span><span>Current multiple <b>{item.current_multiple == null ? "Unavailable" : `${show(item.current_multiple, 3)}×`}</b></span><span>Source time <b>{time(item.latest_observation_timestamp)}</b></span><span>Received <b>{time(item.latest_observation_ingested_at)}</b></span></div><div className="target-levels">{item.targets?.map((target) => <div className={`target-level ${target.status === "REACHED" ? "reached" : ""}`} key={target.multiplier}><span>{target.multiplier}X</span><strong>₹{show(target.target_value)}</strong><small>{target.status === "REACHED" ? `First reached ${time(target.first_reached_at)}` : target.status ?? "UNVERIFIED"}</small></div>)}</div>{item.status === "active" && <button type="button" className="text-button" onClick={() => void cancel(item.session_id)}>Stop tracking</button>}</article>)}</div> : !loading && <div className="empty-state-panel compact-empty"><span className="empty-state-mark">◎</span><div><strong>{error ? "Target data unavailable" : "No tracking sessions yet"}</strong><p>{error || "Start only after a fresh Dhan observation is shown and explicitly confirmed."}</p></div><span className="status-tag">{error ? "DATA UNAVAILABLE" : "EMPTY"}</span></div>}
    {!compact && <p className="panel-footnote">Targets are monitoring levels, not predictions or guarantees. Stale or missing live observations remain unverified.</p>}
  </section>;
}

export function WatchlistPanel({ accessToken, apiBase }: { accessToken: string; apiBase: string }) {
  const [watchlists, setWatchlists] = useState<WatchlistEntry[]>([]);
  const [selectedList, setSelectedList] = useState("");
  const [name, setName] = useState("My Watchlist");
  const [underlyings, setUnderlyings] = useState<string[]>([]);
  const [expiries, setExpiries] = useState<string[]>([]);
  const [kind, setKind] = useState<"underlying" | "option">("underlying");
  const [form, setForm] = useState({ underlying: "", expiry: "", strike: "", option_type: "CALL" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const headers = { Authorization: `Bearer ${accessToken}` };

  const loadLists = useCallback(async () => {
    try {
      const response = await authenticatedFetch(`${apiBase}/watchlists`, { cache: "no-store", headers });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "Watchlists are unavailable.");
      const values = (body.watchlists ?? []) as WatchlistEntry[];
      setWatchlists(values);
      setSelectedList((current) => values.some((item) => item.id === current) ? current : values[0]?.id ?? "");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Watchlists are unavailable."); }
  }, [apiBase, accessToken]);

  useEffect(() => {
    void loadLists();
    const timer = window.setInterval(() => void loadLists(), 10000);
    return () => window.clearInterval(timer);
  }, [loadLists]);
  useEffect(() => {
    let active = true;
    void authenticatedFetch(`${apiBase}/options/underlyings`, { cache: "no-store", headers })
      .then(async (response) => { if (!response.ok) throw new Error("Dhan instrument mappings are unavailable."); return response.json() as Promise<{ underlyings: OptionUnderlying[] }>; })
      .then((body) => { if (active) { const values = (body.underlyings ?? []).map((item) => item.symbol); setUnderlyings(values); setForm((current) => ({ ...current, underlying: current.underlying || values[0] || "" })); } })
      .catch((cause) => { if (active) setError(cause instanceof Error ? cause.message : "Dhan instruments unavailable."); });
    return () => { active = false; };
  }, [apiBase, accessToken]);
  useEffect(() => {
    if (!form.underlying) { setExpiries([]); return; }
    let active = true;
    void authenticatedFetch(`${apiBase}/options/${encodeURIComponent(form.underlying)}/expiries`, { cache: "no-store", headers })
      .then(async (response) => { if (!response.ok) throw new Error("Dhan expiry data is unavailable."); return response.json() as Promise<{ available: boolean; expiries: string[] }>; })
      .then((body) => { if (active) { const values = body.available ? body.expiries ?? [] : []; setExpiries(values); setForm((current) => ({ ...current, expiry: values[0] ?? "" })); } })
      .catch((cause) => { if (active) setError(cause instanceof Error ? cause.message : "Dhan expiries unavailable."); });
    return () => { active = false; };
  }, [apiBase, accessToken, form.underlying]);

  const createList = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const response = await authenticatedFetch(`${apiBase}/watchlists`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "Could not create watchlist.");
      await loadLists(); setSelectedList(body.id); setName("");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Watchlist creation failed."); }
    finally { setBusy(false); }
  };

  const addItem = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (!selectedList) return;
    setBusy(true); setError("");
    try {
      let subject: Record<string, string>;
      if (kind === "option") {
        subject = { underlying: form.underlying, expiry: form.expiry, strike: form.strike, option_type: form.option_type };
      } else {
        const response = await authenticatedFetch(`${apiBase}/instruments?underlying=${encodeURIComponent(form.underlying)}&limit=500`, { cache: "no-store", headers });
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail ?? "Dhan underlying mappings are unavailable.");
        const instrument = (body.instruments ?? []).find((item: { exchange_segment: string; is_active: boolean; is_expired: boolean; id: string }) => ["IDX_I", "NSE_EQ"].includes(item.exchange_segment) && item.is_active && !item.is_expired);
        if (!instrument) throw new Error("No active Dhan-confirmed underlying mapping is available.");
        subject = { instrument_id: instrument.id };
      }
      const response = await authenticatedFetch(`${apiBase}/watchlists/${encodeURIComponent(selectedList)}/items`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify(subject) });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "Could not add this Dhan-mapped instrument.");
      await loadLists();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not add this watchlist item."); }
    finally { setBusy(false); }
  };

  const removeItem = async (itemId: string) => {
    if (!selectedList) return;
    try {
      const response = await authenticatedFetch(`${apiBase}/watchlists/${encodeURIComponent(selectedList)}/items/${encodeURIComponent(itemId)}`, { method: "DELETE", headers });
      if (!response.ok) throw new Error("Could not remove this watchlist item.");
      await loadLists();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not remove the item."); }
  };

  const selected = watchlists.find((list) => list.id === selectedList);
  return <section className="workspace-panel watchlist-panel"><div className="content-heading"><div><p className="eyebrow">USER OWNED · DHANHQ MAPPINGS</p><h2>Watchlists</h2><p className="panel-description">Watchlist items resolve to active Dhan instruments. Quotes include separate exchange and receive timestamps.</p></div><span className="status-tag">{watchlists.length} LISTS</span></div>
    {error && <div className="inline-error" role="alert">{error}</div>}
    <div className="watchlist-tools"><form className="watchlist-create" onSubmit={createList}><label>Create a watchlist<input required maxLength={80} value={name} onChange={(event) => setName(event.target.value)} placeholder="Watchlist name" /></label><button type="submit" className="secondary-button" disabled={busy || !name.trim()}>{busy ? "Saving…" : "Create list"}</button></form>{watchlists.length > 0 && <label className="watchlist-select-label">Selected list<select value={selectedList} onChange={(event) => setSelectedList(event.target.value)}>{watchlists.map((list) => <option key={list.id} value={list.id}>{list.name}{list.is_default ? " · Default" : ""}</option>)}</select></label>}</div>
    {selectedList && <form className="watchlist-add" onSubmit={addItem}><p className="eyebrow">ADD DHAN MAPPED ITEM</p><div className="watchlist-add-fields"><label>Item type<select value={kind} onChange={(event) => setKind(event.target.value as "underlying" | "option")}><option value="underlying">Underlying</option><option value="option">Option contract</option></select></label><label>Underlying<select required value={form.underlying} onChange={(event) => setForm((current) => ({ ...current, underlying: event.target.value }))}>{underlyings.map((symbol) => <option key={symbol} value={symbol}>{symbol}</option>)}</select></label>{kind === "option" && <><label>Expiry<select required value={form.expiry} onChange={(event) => setForm((current) => ({ ...current, expiry: event.target.value }))}>{expiries.map((expiry) => <option key={expiry} value={expiry}>{expiry}</option>)}</select></label><label>Strike<input required type="number" min="0.00000001" step="any" value={form.strike} onChange={(event) => setForm((current) => ({ ...current, strike: event.target.value }))} /></label><label>Option type<select value={form.option_type} onChange={(event) => setForm((current) => ({ ...current, option_type: event.target.value }))}><option value="CALL">CALL</option><option value="PUT">PUT</option></select></label></>}<button type="submit" className="secondary-button" disabled={busy || !form.underlying || (kind === "option" && (!form.expiry || !form.strike))}>{busy ? "Adding…" : "Add item"}</button></div></form>}
    {selected?.items.length ? <div className="watchlist-items">{selected.items.map((item) => <article className="watchlist-item" key={item.id}><div className="watchlist-item-name"><strong>{item.underlying}{item.strike != null ? ` · ${show(item.strike, 0)} ${item.option_type ?? ""}` : ""}</strong><small>{item.expiry ? `Expiry ${item.expiry} · ` : ""}{item.display_name ?? item.underlying}</small></div><div className="watchlist-live-value"><strong>{item.quote.last_price == null ? "Unavailable" : `₹${show(item.quote.last_price)}`}</strong><span className={`status-tag ${item.quote.status === "LIVE" ? "tag-live" : ""}`}>{item.quote.status}</span><small>Exchange {time(item.quote.provider_timestamp)} · Received {time(item.quote.ingested_at)}</small></div><button type="button" className="text-button" onClick={() => void removeItem(item.id)} aria-label={`Remove ${item.underlying} from watchlist`}>Remove</button></article>)}</div> : selectedList ? <div className="empty-state-panel compact-empty"><span className="empty-state-mark">☆</span><div><strong>Watchlist is empty</strong><p>Add an active Dhan-mapped underlying or option. No prices are displayed until the backend receives a quote.</p></div><span className="status-tag">EMPTY</span></div> : !watchlists.length ? <div className="empty-state-panel compact-empty"><span className="empty-state-mark">☆</span><div><strong>No watchlists yet</strong><p>Create a watchlist to save provider-confirmed market instruments.</p></div><span className="status-tag">EMPTY</span></div> : null}
  </section>;
}

type UserAlert = { id: string; notification_type: string; title: string; body: string; payload: Record<string, unknown>; read_at: string | null; created_at: string };

export function AlertsPanel({ accessToken, apiBase, streamStatus }: { accessToken: string; apiBase: string; streamStatus: string }) {
  const [alerts, setAlerts] = useState<UserAlert[]>([]);
  const [unread, setUnread] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const loadAlerts = useCallback(async () => {
    const response = await authenticatedFetch(`${apiBase}/alerts?limit=100`, {
      cache: "no-store", headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (!response.ok) throw new Error(response.status === 503 ? "Alerts are unavailable until the database is connected." : "Could not load your alert inbox.");
    const result = await response.json() as { notifications: UserAlert[]; unread_count: number };
    setAlerts(result.notifications ?? []);
    setUnread(result.unread_count ?? 0);
  }, [apiBase, accessToken]);

  useEffect(() => {
    let active = true;
    void loadAlerts().catch((cause) => { if (active) setError(cause instanceof Error ? cause.message : "Alert inbox unavailable."); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [loadAlerts]);

  useEffect(() => {
    const refresh = () => void loadAlerts().catch(() => setError("A live alert arrived, but the inbox could not be refreshed."));
    window.addEventListener("market-pulse:target-alert", refresh);
    return () => window.removeEventListener("market-pulse:target-alert", refresh);
  }, [loadAlerts]);

  const markRead = async (alert: UserAlert) => {
    if (alert.read_at) return;
    setError("");
    try {
      const response = await authenticatedFetch(`${apiBase}/alerts/${alert.id}/read`, {
        method: "POST", headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!response.ok) throw new Error("Could not mark this alert as read.");
      const body = await response.json() as { read_at: string };
      setAlerts((items) => items.map((item) => item.id === alert.id ? { ...item, read_at: body.read_at } : item));
      setUnread((count) => Math.max(0, count - 1));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Alert update unavailable."); }
  };

  return <section className="workspace-panel alerts-panel">
    <div className="content-heading"><div><p className="eyebrow">DURABLE IN-APP ALERTS</p><h2>Your alerts</h2><p className="panel-description">Target hits are saved before a user-scoped live update is sent.</p></div><div className="alerts-status"><span className={`status-tag ${streamStatus === "LIVE" ? "tag-live" : ""}`}>{streamStatus}</span><span className="status-tag">{unread} UNREAD</span></div></div>
    {error && <div className="inline-error" role="alert">{error}</div>}
    {loading ? <div className="empty-state-panel" role="status"><span className="empty-state-mark">⌁</span><div><strong>Loading saved alerts</strong><p>Reading your authenticated inbox.</p></div><span className="status-tag">LOADING</span></div> : alerts.length ? <div className="alerts-list" aria-live="polite">{alerts.map((alert) => <article className={`alert-item ${alert.read_at ? "is-read" : "is-unread"}`} key={alert.id}><span className="alert-mark" aria-hidden="true">{alert.read_at ? "◇" : "◆"}</span><div className="alert-copy"><strong>{alert.title}</strong><p>{alert.body}</p><small>Provider observation {time(typeof alert.payload.provider_timestamp === "string" ? alert.payload.provider_timestamp : null)} · Saved {time(alert.created_at)}</small></div>{!alert.read_at && <button type="button" className="text-button" onClick={() => void markRead(alert)}>Mark read</button>}</article>)}</div> : <div className="empty-state-panel"><span className="empty-state-mark">◇</span><div><strong>No target alerts yet</strong><p>When a fresh Dhan observation first crosses a tracked target, the event is saved here. Alerts are never generated from stale cached prices.</p></div><span className="status-tag">EMPTY</span></div>}
  </section>;
}
