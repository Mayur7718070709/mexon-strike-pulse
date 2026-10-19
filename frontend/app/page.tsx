"use client";

import { FormEvent, useEffect, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase } from "../lib/supabase";

type Quote = { security_id: string; exchange_segment: string; last_price: number; exchange_timestamp: string; received_at: string; status: "LIVE" | "DATA STALE" };
type MarketStatus = { status: "LIVE" | "DATA STALE" | "DATA UNAVAILABLE"; source: string; quotes: Quote[]; message?: string };
const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1";

function formatTime(value?: string) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Asia/Kolkata" }).format(new Date(value));
}

export default function Home() {
  const [market, setMarket] = useState<MarketStatus | null>(null);
  const [connection, setConnection] = useState<"loading" | "connected" | "unavailable">("loading");
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
    if (!session?.access_token) return;
    let active = true;
    const socketUrl = `${apiBase.replace(/^http/, "ws")}/ws/market`;
    const socket = new WebSocket(socketUrl, ["market-pulse", `access-token.${session.access_token}`]);
    socket.onmessage = (event) => {
      try {
        const quote = JSON.parse(event.data) as Quote;
        if (!quote.security_id || typeof quote.last_price !== "number") return;
        quote.status = "LIVE";
        setMarket((previous) => ({ status: "LIVE", source: "DHANHQ", quotes: [...(previous?.quotes.filter((item) => item.security_id !== quote.security_id) ?? []), quote] }));
        setConnection("connected");
      } catch { /* Ignore malformed server frames; the REST status remains authoritative. */ }
    };
    const refresh = async () => {
      try {
        const response = await fetch(`${apiBase}/market/status`, { cache: "no-store", headers: { Authorization: `Bearer ${session.access_token}` } });
        if (!response.ok) throw new Error("API unavailable");
        const result = (await response.json()) as MarketStatus;
        if (active) { setMarket(result); setConnection("connected"); }
      } catch {
        if (active) { setConnection("unavailable"); setMarket({ status: "DATA UNAVAILABLE", source: "DHANHQ", quotes: [], message: "Market API is unreachable." }); }
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => { active = false; window.clearInterval(timer); socket.close(); };
  }, [session?.access_token]);

  const signIn = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setAuthError("");
    if (!supabase) { setAuthError("Supabase Auth is not configured for this deployment."); return; }
    setAuthBusy(true);
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) setAuthError(error.message);
    setAuthBusy(false);
  };

  if (authLoading) return <main className="auth-shell"><section className="auth-card"><Brand /><p className="subhead">Restoring your secure session…</p></section></main>;
  if (!session) return <main className="auth-shell"><section className="auth-card">
    <Brand /><p className="eyebrow auth-eyebrow">MEXON INTELLIGENCE</p><h1>Sign in to Market Pulse</h1><p className="auth-copy">Access live market intelligence with your authorized account.</p>
    <form onSubmit={signIn} className="auth-form"><label htmlFor="email">Email address</label><input id="email" type="email" autoComplete="username" value={email} onChange={(event) => setEmail(event.target.value)} required /><label htmlFor="password">Password</label><input id="password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /><button disabled={authBusy || !supabase}>{authBusy ? "Signing in…" : "Sign in"}</button></form>
    {authError && <p className="auth-error" role="alert">{authError}</p>}{!supabase && <p className="auth-error" role="status">Supabase Auth is not configured. Set the public Supabase URL and anon key.</p>}
    <p className="auth-foot">Market data is provided by DhanHQ. Your session is protected by Supabase Auth.</p>
  </section></main>;

  const status = connection === "unavailable" ? "DATA UNAVAILABLE" : market?.status ?? "DATA UNAVAILABLE";
  return <main className="shell">
    <header className="topbar">
      <Brand />
      <nav aria-label="Primary navigation"><a className="active" href="#overview">Overview</a><a href="#watchlist">Watchlist</a><a href="#options">Options</a></nav>
      <button className="profile" aria-label="Sign out" title="Sign out" onClick={() => void supabase?.auth.signOut()}>{session.user.email?.slice(0, 2).toUpperCase() ?? "MI"}</button>
    </header>

    <section className="intro" id="overview">
      <div><p className="eyebrow">NSE MARKET <span>·</span> LIVE DATA</p><h1>Market overview</h1><p className="subhead">Your live view of the market, in one place.</p></div>
      <div className={`feed-pill ${status === "LIVE" ? "is-live" : ""}`}><span className="feed-dot" />{status}<span className="feed-source">DHANHQ</span></div>
    </section>

    <section className="notice" role="status" aria-live="polite">
      <span className="notice-icon">i</span><span><strong>{status}</strong>{market?.message ? ` — ${market.message}` : status === "DATA STALE" ? " — Last received quotes are outside the freshness window." : status === "LIVE" ? " — Receiving DhanHQ market feed." : " — Waiting for a verified DhanHQ market feed."}</span>
      <span className="updated">Updated {formatTime(market?.quotes[0]?.received_at)}</span>
    </section>

    <section className="section-heading" id="watchlist"><div><p className="eyebrow">YOUR MARKETS</p><h2>Watchlist</h2></div><span className="count">{market?.quotes.length ?? 0} instruments</span></section>
    <section className="quote-grid" aria-label="Live market quotes">
      {market?.quotes.length ? market.quotes.map((quote) => <article className="quote-card" key={quote.security_id}>
        <div className="quote-card-top"><span className="instrument-icon">◈</span><span className={`mini-status ${quote.status === "LIVE" ? "is-live" : ""}`}><i />{quote.status}</span></div>
        <p className="symbol">Security {quote.security_id}</p><p className="exchange">DHANHQ · {quote.exchange_segment}</p>
        <div className="price-row"><strong>{quote.last_price.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong><span>INR</span></div>
        <div className="card-footer"><span>Exchange time</span><span>{formatTime(quote.exchange_timestamp)}</span></div>
      </article>) : <article className="empty-card"><span className="empty-mark">⌁</span><div><strong>{connection === "loading" ? "Connecting to market data" : "Your watchlist is ready"}</strong><p>{market?.message ?? "Live instruments will appear here when DhanHQ data is available."}</p></div><span className="empty-state">{status}</span></article>}
    </section>

    <section className="bottom-grid" id="options">
      <article className="panel"><div className="panel-title"><div><p className="eyebrow">MARKET SNAPSHOT</p><h2>Index performance</h2></div><span className="source-label">DHANHQ LIVE</span></div><div className="empty-chart"><div className="chart-lines"><i /><i /><i /><i /></div><span>{status === "LIVE" ? "Index instruments are not configured" : "Live index data will appear here"}</span></div></article>
      <article className="panel options-panel"><div className="panel-title"><div><p className="eyebrow">DERIVATIVES</p><h2>Options pulse</h2></div><span className="options-glyph">⌘</span></div><div className="options-placeholder"><div className="option-row"><span>Put / Call ratio</span><b>—</b></div><div className="option-row"><span>Max pain</span><b>—</b></div><div className="option-row"><span>Open interest</span><b>—</b></div><p>Configure live option instruments to enable this view.</p></div></article>
    </section>
    <footer><span>© 2026 Mexon Intelligence Pvt Ltd</span><span><i className={`footer-dot ${status === "LIVE" ? "is-live" : ""}`} />{status} · Source: DhanHQ v2</span></footer>
  </main>;
}

function Brand() {
  return <a className="brand" href="#overview" aria-label="Market Pulse home"><span className="brand-mark">M</span><span>MARKET<span className="brand-light">PULSE</span><small>BY MEXON INTELLIGENCE</small></span></a>;
}
