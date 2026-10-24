"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase } from "../lib/supabase";
import { authenticatedFetch } from "../lib/api";
import { OptionChainPanel } from "./option-chain";
import { HistoricalAdminPanel } from "./historical-admin";
import { PastPresentNextFuturePanel } from "./analysis";
import { AlertsPanel, ScannerPanel, TargetTrackerPanel, WatchlistPanel } from "./product-panels";

type Screen = "dashboard" | "market" | "options" | "scanner" | "historical" | "analysis" | "watchlist" | "targets" | "alerts" | "profile" | "settings";
type Quote = { security_id: string; exchange_segment: string; last_price: number; exchange_timestamp: string; received_at: string; status: "LIVE" | "DATA STALE" };
type MarketStatus = { status: "LIVE" | "DATA STALE" | "CONNECTION LOST" | "DATA UNAVAILABLE"; source: string; quotes: Quote[]; message?: string };
type DhanStatus = {
  provider: string;
  status: string;
  connected: boolean;
  checked_at: string;
  message: string;
  feed: { connected: boolean; last_event_at?: string | null; last_error_type?: string | null };
  token_expires_at?: string | null;
  active_segments?: string | null;
  data_plan_status?: string | null;
  data_valid_until?: string | null;
  last_received_at?: string | null;
  feed_data_quality?: string | null;
};

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1";
const navigation: { id: Screen; label: string; icon: string; group: string }[] = [
  { id: "dashboard", label: "Dashboard", icon: "⌂", group: "WORKSPACE" },
  { id: "market", label: "Market overview", icon: "◫", group: "WORKSPACE" },
  { id: "options", label: "Option chain", icon: "⇄", group: "MARKET TOOLS" },
  { id: "scanner", label: "10X Scanner", icon: "⌕", group: "MARKET TOOLS" },
  { id: "historical", label: "Historical analysis", icon: "▤", group: "MARKET TOOLS" },
  { id: "analysis", label: "Past / Present / Next / Future", icon: "◷", group: "MARKET TOOLS" },
  { id: "watchlist", label: "Watchlist", icon: "☆", group: "MY DATA" },
  { id: "targets", label: "Target tracker", icon: "◎", group: "MY DATA" },
  { id: "alerts", label: "Alerts", icon: "♢", group: "MY DATA" },
  { id: "profile", label: "Profile", icon: "◉", group: "ACCOUNT" },
  { id: "settings", label: "Settings", icon: "⚙", group: "ACCOUNT" },
];

function formatTime(value?: string) {
  if (!value || Number.isNaN(new Date(value).getTime())) return "Unavailable";
  return new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeStyle: "medium", timeZone: "Asia/Kolkata" }).format(new Date(value));
}

export default function Home() {
  const [screen, setScreen] = useState<Screen>("dashboard");
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [market, setMarket] = useState<MarketStatus | null>(null);
  const [connection, setConnection] = useState<"LOADING" | "LIVE" | "STALE" | "CONNECTION LOST" | "DATA UNAVAILABLE">("LOADING");
  const [alertStream, setAlertStream] = useState<"CONNECTING" | "LIVE" | "CONNECTION LOST">("CONNECTING");
  const [latestAlert, setLatestAlert] = useState<{ title: string; body: string } | null>(null);
  const seenNotificationIds = useRef(new Set<string>());
  const [session, setSession] = useState<Session | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [authError, setAuthError] = useState("");
  const [authBusy, setAuthBusy] = useState(false);

  useEffect(() => {
    if (!supabase) { setAuthLoading(false); return; }
    void supabase.auth.getSession().then(({ data }) => { setSession(data.session); setAuthLoading(false); });
    const { data: listener } = supabase.auth.onAuthStateChange((_event, nextSession) => setSession(nextSession));
    return () => listener.subscription.unsubscribe();
  }, []);

  useEffect(() => {
    const expireSession = () => { void supabase?.auth.signOut(); };
    window.addEventListener("market-pulse:session-expired", expireSession);
    return () => window.removeEventListener("market-pulse:session-expired", expireSession);
  }, []);

  useEffect(() => {
    if (!session?.access_token) return;
    let active = true;
    let socketConnected = false;
    const socketUrl = `${apiBase.replace(/^http/, "ws")}/ws/market`;
    const socket = new WebSocket(socketUrl, ["market-pulse", `access-token.${session.access_token}`]);
    socket.onopen = () => { socketConnected = true; };
    socket.onmessage = (event) => {
      try {
        const quote = JSON.parse(event.data) as Quote;
        if (!quote.security_id || typeof quote.last_price !== "number" || !Number.isFinite(quote.last_price)) return;
        setMarket((previous) => ({ status: "LIVE", source: "DHANHQ", quotes: [...(previous?.quotes.filter((item) => item.security_id !== quote.security_id) ?? []), { ...quote, status: "LIVE" }] }));
        setConnection("LIVE");
      } catch { /* Malformed frames are ignored; REST status provides the current dependency state. */ }
    };
    socket.onclose = () => { socketConnected = false; if (active) setConnection((current) => current === "LIVE" ? "CONNECTION LOST" : current); };
    socket.onerror = () => { if (active) setConnection((current) => current === "LIVE" ? "CONNECTION LOST" : current); };
    const refresh = async () => {
      try {
        const response = await authenticatedFetch(`${apiBase}/market/status`, { cache: "no-store", headers: { Authorization: `Bearer ${session.access_token}` } });
        if (response.status === 401) { await supabase?.auth.signOut(); return; }
        if (!response.ok) throw new Error("Market API unavailable");
        const result = await response.json() as MarketStatus;
        if (!active) return;
        setMarket(result);
        if (result.status === "LIVE") setConnection("LIVE");
        else if (result.status === "DATA STALE") setConnection("STALE");
        else if (result.status === "CONNECTION LOST") setConnection("CONNECTION LOST");
        else setConnection(socketConnected ? "CONNECTION LOST" : "DATA UNAVAILABLE");
      } catch {
        if (active) {
          setConnection(socketConnected ? "CONNECTION LOST" : "DATA UNAVAILABLE");
          setMarket((previous) => previous?.quotes.length
            ? { ...previous, status: "CONNECTION LOST", message: "Market API is unreachable. The last observation is retained with its original timestamps." }
            : { status: "DATA UNAVAILABLE", source: "DHANHQ", quotes: [], message: "Market API is unreachable; no market values are available." });
        }
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { active = false; window.clearInterval(timer); socket.close(); };
  }, [session?.access_token]);

  useEffect(() => {
    if (!session?.access_token) { setAlertStream("CONNECTING"); return; }
    let active = true;
    let reconnectTimer = 0;
    let dismissTimer = 0;
    let socket: WebSocket | null = null;
    const connect = () => {
      if (!active) return;
      socket = new WebSocket(`${apiBase.replace(/^http/, "ws")}/ws/alerts`, ["market-pulse", `access-token.${session.access_token}`]);
      socket.onopen = () => { if (active) setAlertStream("LIVE"); };
      socket.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data) as { type?: string; notification_id?: string; title?: string; body?: string };
          if (message.type !== "target_alert" || !message.title || !message.body) return;
          if (message.notification_id && seenNotificationIds.current.has(message.notification_id)) return;
          if (message.notification_id) seenNotificationIds.current.add(message.notification_id);
          setLatestAlert({ title: message.title, body: message.body });
          window.clearTimeout(dismissTimer);
          dismissTimer = window.setTimeout(() => setLatestAlert(null), 10000);
          window.dispatchEvent(new Event("market-pulse:target-alert"));
        } catch { /* Inbox REST remains authoritative if a socket frame is malformed. */ }
      };
      socket.onerror = () => { if (active) setAlertStream("CONNECTION LOST"); };
      socket.onclose = (event) => {
        if (!active) return;
        if (event.code === 1008) {
          window.dispatchEvent(new Event("market-pulse:session-expired"));
          return;
        }
        setAlertStream("CONNECTION LOST");
        reconnectTimer = window.setTimeout(connect, 3000);
      };
    };
    connect();
    return () => { active = false; window.clearTimeout(reconnectTimer); window.clearTimeout(dismissTimer); socket?.close(); };
  }, [session?.access_token]);

  const signIn = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setAuthError("");
    if (!supabase) { setAuthError("Supabase Auth is not configured for this deployment."); return; }
    setAuthBusy(true);
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) setAuthError(error.message);
    setAuthBusy(false);
  };

  const latestReceived = useMemo(() => market?.quotes.map((item) => item.received_at).filter(Boolean).sort().at(-1), [market]);
  const status = session ? connection : "DATA UNAVAILABLE";

  if (authLoading) return <main className="auth-shell"><section className="auth-card"><Brand /><p className="subhead">Restoring your secure session…</p></section></main>;
  if (!session) return <main className="auth-shell"><section className="auth-card">
    <Brand /><p className="eyebrow auth-eyebrow">MEXON INTELLIGENCE</p><h1>Sign in to Market Pulse</h1><p className="auth-copy">Access live market intelligence with your authorized account.</p>
    <form onSubmit={signIn} className="auth-form"><label htmlFor="email">Email address</label><input id="email" type="email" autoComplete="username" value={email} onChange={(event) => setEmail(event.target.value)} required /><label htmlFor="password">Password</label><input id="password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /><button disabled={authBusy || !supabase}>{authBusy ? "Signing in…" : "Sign in"}</button></form>
    {authError && <p className="auth-error" role="alert">{authError}</p>}{!supabase && <p className="auth-error" role="status">Supabase Auth is not configured. Set the public Supabase URL and anon key.</p>}
    <p className="auth-foot">Market data is provided by DhanHQ. Your session is protected by Supabase Auth.</p>
  </section></main>;

  const title = navigation.find((item) => item.id === screen)?.label ?? "Dashboard";
  const isAdmin = session.user.app_metadata?.role === "admin";
  const unavailableFeature = (feature: string, endpoint: string) => <section className="workspace-panel capability-empty"><span className="capability-icon">⌁</span><div><p className="eyebrow">BACKEND CAPABILITY</p><h2>{feature}</h2><p>This screen is waiting for the authenticated FastAPI endpoint <code>{endpoint}</code>. No sample or local-only records are shown.</p><span className="status-tag">DATA UNAVAILABLE</span></div></section>;

  return <main className="app-shell">
    <aside className="sidebar" aria-label="Application sidebar">
      <Brand />
      <div className="sidebar-scroll">{[...new Set(navigation.map((item) => item.group))].map((group) => <div className="nav-group" key={group}>
        <p className="nav-group-label">{group}</p>{navigation.filter((item) => item.group === group).map((item) => <button key={item.id} type="button" className={`nav-item ${screen === item.id ? "selected" : ""}`} onClick={() => setScreen(item.id)} aria-current={screen === item.id ? "page" : undefined}><span aria-hidden="true">{item.icon}</span>{item.label}</button>)}
      </div>)}</div>
      <div className="sidebar-bottom"><div className="sidebar-status"><i className={`status-indicator state-${status.toLowerCase().replaceAll(" ", "-")}`} /><span>{status}</span><small>DHANHQ · {formatTime(latestReceived)}</small></div><button type="button" className="signout-button" onClick={() => void supabase?.auth.signOut()}><span className="avatar">{session.user.email?.slice(0, 2).toUpperCase() ?? "MI"}</span><span className="account-label">{session.user.email ?? "Account"}<small>Sign out</small></span><span aria-hidden="true">↗</span></button></div>
    </aside>

    <div className="main-column">
      <header className="mobile-header"><Brand /><button type="button" aria-label="Sign out" onClick={() => void supabase?.auth.signOut()} className="avatar">{session.user.email?.slice(0, 2).toUpperCase() ?? "MI"}</button></header>
      <header className="page-topbar"><div><p className="eyebrow">MARKET PULSE <span>·</span> DHANHQ DATA</p><h1>{title}</h1></div><div className={`global-status status-${status.toLowerCase().replaceAll(" ", "-")}`}><i />{status}</div></header>

      <div className="content-area">
        {(screen === "dashboard" || screen === "market") && <>
          <section className="welcome-row"><div><p className="eyebrow">{screen === "market" ? "NSE MARKET" : "MARKET SESSION"}</p><h2>{screen === "market" ? "Market overview" : "Your market, at a glance."}</h2><p>Provider-confirmed observations only. Prices retain their source timestamps.</p></div><div className="last-update"><span>LAST RECEIVED</span><strong>{formatTime(latestReceived)}</strong></div></section>
          <section className="metric-grid" aria-label="Market status summary"><article className="metric-card"><span>FEED STATUS</span><strong className={`metric-${status.toLowerCase().replaceAll(" ", "-")}`}>{status}</strong><small>Verified backend feed state</small></article><article className="metric-card"><span>OBSERVATIONS</span><strong>{market?.quotes.length ?? "—"}</strong><small>Quotes currently available</small></article><article className="metric-card"><span>DATA SOURCE</span><strong>DHANHQ</strong><small>Live values only; no simulation</small></article></section>
          <section className="workspace-panel quotes-panel"><div className="content-heading"><div><p className="eyebrow">MARKET SNAPSHOT</p><h2>Live observations</h2></div><span className={`status-tag ${status === "LIVE" ? "tag-live" : ""}`}>{status}</span></div>
            {market?.quotes.length ? <div className="live-quote-grid">{market.quotes.map((quote) => <article className="live-quote" key={`${quote.exchange_segment}:${quote.security_id}`}><div className="quote-heading"><span className="quote-symbol">{quote.security_id}</span><span className="quote-segment">{quote.exchange_segment}</span></div><strong className="quote-price">₹{quote.last_price.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong><div className="quote-times"><span>Source time <b>{formatTime(quote.exchange_timestamp)}</b></span><span>Received <b>{formatTime(quote.received_at)}</b></span></div><span className={`quote-freshness ${quote.status === "LIVE" && status === "LIVE" ? "fresh" : ""}`}>{status === "LIVE" ? quote.status : status}</span></article>)}</div> : <EmptyState title={status === "LOADING" ? "Connecting to DhanHQ" : "No live observations"} message={market?.message ?? "No provider-confirmed quotes are available. Instruments will appear only when returned by the Dhan feed."} status={status} />}
          </section>
          {screen === "dashboard" && <><ScannerPanel accessToken={session.access_token} apiBase={apiBase} compact /><div className="dashboard-two-col"><TargetTrackerPanel accessToken={session.access_token} apiBase={apiBase} compact /></div></>}
        </>}

        {screen === "options" && <OptionChainPanel accessToken={session.access_token} apiBase={apiBase} />}
        {screen === "watchlist" && <WatchlistPanel accessToken={session.access_token} apiBase={apiBase} />}
        {screen === "scanner" && <ScannerPanel accessToken={session.access_token} apiBase={apiBase} />}
        {screen === "analysis" && <PastPresentNextFuturePanel accessToken={session.access_token} apiBase={apiBase} />}
        {screen === "historical" && (isAdmin ? <HistoricalAdminPanel accessToken={session.access_token} apiBase={apiBase} /> : unavailableFeature("Historical analysis and coverage", "GET /api/v1/admin/historical/coverage"))}
        {screen === "targets" && <TargetTrackerPanel accessToken={session.access_token} apiBase={apiBase} />}
        {screen === "alerts" && <AlertsPanel accessToken={session.access_token} apiBase={apiBase} streamStatus={alertStream} />}
        {screen === "profile" && <section className="workspace-panel profile-panel"><div className="content-heading"><div><p className="eyebrow">AUTHENTICATED ACCOUNT</p><h2>Profile</h2></div><span className="status-tag tag-live">SUPABASE AUTH</span></div><dl><div><dt>Email</dt><dd>{session.user.email ?? "Unavailable"}</dd></div><div><dt>User ID</dt><dd>{session.user.id}</dd></div><div><dt>Session</dt><dd>Protected Supabase session · expires {formatTime(session.expires_at ? new Date(session.expires_at * 1000).toISOString() : undefined)}</dd></div></dl><button type="button" className="secondary-button" onClick={() => void supabase?.auth.signOut()}>Sign out securely</button></section>}
        {screen === "settings" && <><DhanStatusPanel accessToken={session.access_token} apiBase={apiBase} />{unavailableFeature("Personal preferences", "GET /api/v1/user/preferences")}<section className="workspace-panel settings-security"><p className="eyebrow">SESSION SECURITY</p><h2>Authentication</h2><p>Market API requests use your Supabase access token. Expired or rejected sessions return to sign-in. Dhan credentials remain on the backend.</p><button type="button" className="secondary-button" onClick={() => void supabase?.auth.signOut()}>Sign out securely</button></section></>}
      </div>
      <footer className="app-footer"><span>© 2026 Mexon Intelligence Pvt Ltd</span><span><i className={`status-indicator state-${status.toLowerCase().replaceAll(" ", "-")}`} />{status} · DhanHQ v2</span></footer>
      {latestAlert && <aside className="target-alert-toast" role="status" aria-live="assertive"><span aria-hidden="true">◆</span><div><strong>{latestAlert.title}</strong><p>{latestAlert.body}</p><button type="button" className="text-button" onClick={() => { setScreen("alerts"); setLatestAlert(null); }}>Open alerts</button></div><button type="button" className="toast-dismiss" aria-label="Dismiss alert notification" onClick={() => setLatestAlert(null)}>×</button></aside>}
      {mobileMenuOpen && <nav className="mobile-more-menu" aria-label="More screens">{navigation.filter((item) => !["dashboard", "options", "scanner", "watchlist"].includes(item.id)).map((item) => <button key={item.id} type="button" onClick={() => { setScreen(item.id); setMobileMenuOpen(false); }}><span aria-hidden="true">{item.icon}</span>{item.label}</button>)}</nav>}
      <nav className="mobile-nav" aria-label="Mobile navigation">{navigation.filter((item) => ["dashboard", "options", "scanner", "watchlist"].includes(item.id)).map((item) => <button key={item.id} type="button" onClick={() => { setScreen(item.id); setMobileMenuOpen(false); }} className={screen === item.id ? "selected" : ""} aria-current={screen === item.id ? "page" : undefined}><span aria-hidden="true">{item.icon}</span>{item.label}</button>)}<button type="button" className={mobileMenuOpen || !["dashboard", "options", "scanner", "watchlist"].includes(screen) ? "selected" : ""} aria-expanded={mobileMenuOpen} aria-label="More screens" onClick={() => setMobileMenuOpen((open) => !open)}><span aria-hidden="true">⋯</span>More</button></nav>
    </div>
  </main>;
}

function EmptyState({ title, message, status }: { title: string; message: string; status: string }) {
  return <div className="empty-state-panel" role="status" aria-live="polite"><span className="empty-state-mark">⌁</span><div><strong>{title}</strong><p>{message}</p></div><span className="status-tag">{status}</span></div>;
}

function DhanStatusPanel({ accessToken, apiBase }: { accessToken: string; apiBase: string }) {
  const [result, setResult] = useState<DhanStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    void authenticatedFetch(`${apiBase}/integrations/dhan/status`, {
      cache: "no-store",
      headers: { Authorization: `Bearer ${accessToken}` },
    }).then(async (response) => {
      if (!response.ok) throw new Error(response.status === 401 ? "Market Pulse session expired. Sign in again to check Dhan." : `Status check failed (HTTP ${response.status}).`);
      const data = await response.json() as DhanStatus;
      if (active) setResult(data);
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "Dhan status is unavailable.");
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [accessToken, apiBase, attempt]);

  return <section className="workspace-panel dhan-status-panel">
    <div className="content-heading"><div><p className="eyebrow">SERVER-SIDE PROVIDER CHECK</p><h2>DhanHQ connection</h2></div><button type="button" className="secondary-button" onClick={() => setAttempt((value) => value + 1)} disabled={loading}>{loading ? "Checking…" : "Check again"}</button></div>
    {error ? <div className="empty-state-panel" role="status"><div><strong>DATA UNAVAILABLE</strong><p>{error}</p></div></div> : loading && !result ? <div className="empty-state-panel" role="status"><div><strong>LOADING</strong><p>Checking Dhan profile through the Market Pulse backend.</p></div></div> : result ? <>
      <div className="dhan-status-message"><span className={`status-tag ${result.connected ? "tag-live" : ""}`}>{result.status}</span><p>{result.message}</p></div>
      <dl className="dhan-status-details">
        <div><dt>Data plan</dt><dd>{result.data_plan_status ?? "Unavailable"}</dd></div>
        <div><dt>Data valid until</dt><dd>{formatTime(result.data_valid_until ?? undefined)}</dd></div>
        <div><dt>Token expires</dt><dd>{formatTime(result.token_expires_at ?? undefined)}</dd></div>
        <div><dt>Active segments</dt><dd>{result.active_segments ?? "Unavailable"}</dd></div>
        <div><dt>Live feed</dt><dd>{result.feed?.connected ? "CONNECTED" : result.feed?.last_error_type ? `DISCONNECTED (${result.feed.last_error_type})` : "NOT RECEIVING"}</dd></div>
        <div><dt>Last observation</dt><dd>{formatTime(result.last_received_at ?? result.feed?.last_event_at ?? undefined)}</dd></div>
      </dl>
      <p className="dhan-status-footnote">Checked {formatTime(result.checked_at)} · credentials are never shown here.</p>
    </> : null}
  </section>;
}

function Brand() {
  return <a className="brand" href="#dashboard" aria-label="Market Pulse"><span className="brand-mark">M</span><span>MARKET<span className="brand-light">PULSE</span><small>BY MEXON INTELLIGENCE</small></span></a>;
}
