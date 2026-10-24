"use client";

import { useCallback, useEffect, useState } from "react";
import { authenticatedFetch } from "../lib/api";
import "./option-chain.css";

type Underlying = { symbol: string; exchange_segment: string; instrument_type: string };
type GreekSet = { delta: number | null; theta: number | null; gamma: number | null; vega: number | null };
type Contract = {
  security_id: string;
  ltp: number | null;
  premium_change: number | null;
  percentage_change: number | null;
  bid: number | null;
  ask: number | null;
  bid_quantity: number | null;
  ask_quantity: number | null;
  volume: number | null;
  open_interest: number | null;
  change_in_oi: number | null;
  implied_volatility: number | null;
  greeks: GreekSet;
};
type StrikeRow = { strike: number; call: Contract | null; put: Contract | null };
type Chain = {
  status: "RECENT RESPONSE" | "DATA STALE" | "DATA UNAVAILABLE";
  available: boolean;
  source: string;
  underlying: string;
  expiry: string;
  underlying_spot?: number;
  source_timestamp?: string;
  provider_timestamp?: string | null;
  timestamp_kind?: string;
  cache_hit?: boolean;
  age_seconds?: number;
  stale?: boolean;
  provider_error?: string;
  message?: string;
  strikes: StrikeRow[];
};

function unavailable(message: string): Chain {
  return { status: "DATA UNAVAILABLE", available: false, source: "DHANHQ", underlying: "", expiry: "", strikes: [], message };
}

function timeLabel(value?: string) {
  if (!value) return "Unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unavailable";
  return new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeStyle: "medium", timeZone: "Asia/Kolkata" }).format(date);
}

function number(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined || !Number.isFinite(value)) return <span className="oc-unavailable" title="Unavailable from Dhan">Unavailable</span>;
  return value.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function ContractCell({ contract }: { contract: Contract | null }) {
  if (!contract) return <div className="oc-contract-empty" aria-label="Contract unavailable">Contract unavailable</div>;
  return <div className="oc-contract">
    <div className="oc-ltp"><span>LTP</span><strong>{number(contract.ltp)}</strong></div>
    <div className="oc-change"><span>Premium Δ</span><b>{number(contract.premium_change)} <small>({number(contract.percentage_change)}%)</small></b></div>
    <div className="oc-quote"><span>Bid <b>{number(contract.bid)}</b><small>Qty {number(contract.bid_quantity, 0)}</small></span><span>Ask <b>{number(contract.ask)}</b><small>Qty {number(contract.ask_quantity, 0)}</small></span></div>
    <div className="oc-stats"><span>Volume <b>{number(contract.volume, 0)}</b></span><span>OI <b>{number(contract.open_interest, 0)}</b></span><span>OI Δ <b>{number(contract.change_in_oi, 0)}</b></span><span>IV <b>{number(contract.implied_volatility, 2)}</b></span></div>
    <div className="oc-greeks" aria-label="Option Greeks"><span>Δ {number(contract.greeks.delta, 4)}</span><span>Θ {number(contract.greeks.theta, 4)}</span><span>Γ {number(contract.greeks.gamma, 5)}</span><span>V {number(contract.greeks.vega, 4)}</span></div>
    <div className="oc-security">Security ID <b>{contract.security_id}</b></div>
  </div>;
}

export function OptionChainPanel({ accessToken, apiBase }: { accessToken: string; apiBase: string }) {
  const [underlyings, setUnderlyings] = useState<Underlying[]>([]);
  const [underlying, setUnderlying] = useState("");
  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiry, setExpiry] = useState("");
  const [chain, setChain] = useState<Chain | null>(null);
  const [loading, setLoading] = useState(false);
  const [expiryLoading, setExpiryLoading] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);

  const headers = { Authorization: `Bearer ${accessToken}` };

  useEffect(() => {
    let active = true;
    void authenticatedFetch(`${apiBase}/options/underlyings`, { headers, cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error("Eligible Dhan option underlyings are unavailable.");
        return response.json() as Promise<{ underlyings: Underlying[] }>;
      })
      .then((result) => {
        if (!active) return;
        setUnderlyings(result.underlyings ?? []);
        setUnderlying((current) => current || result.underlyings?.[0]?.symbol || "");
      })
      .catch(() => { if (active) setUnderlyings([]); });
    return () => { active = false; };
  }, [apiBase, accessToken]);

  useEffect(() => {
    if (!underlying) { setExpiries([]); setExpiry(""); setChain(null); return; }
    let active = true;
    setExpiryLoading(true);
    setExpiries([]);
    setExpiry("");
    setChain(null);
    void authenticatedFetch(`${apiBase}/options/${encodeURIComponent(underlying)}/expiries`, { headers, cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error("Dhan expiries are unavailable.");
        return response.json() as Promise<{ expiries: string[]; available: boolean }>;
      })
      .then((result) => {
        if (!active) return;
        const values = result.available ? result.expiries ?? [] : [];
        setExpiries(values);
        setExpiry(values[0] ?? "");
      })
      .catch(() => { if (active) { setExpiries([]); setChain(unavailable("Dhan expiry information could not be loaded.")); } })
      .finally(() => { if (active) setExpiryLoading(false); });
    return () => { active = false; };
  }, [underlying, apiBase, accessToken]);

  const loadChain = useCallback(async (signal?: AbortSignal) => {
    if (!underlying || !expiry) return;
    setLoading(true);
    try {
      const response = await authenticatedFetch(`${apiBase}/options/${encodeURIComponent(underlying)}/chain?expiry=${encodeURIComponent(expiry)}`, {
        headers, cache: "no-store", signal,
      });
      if (!response.ok) throw new Error(response.status === 429 ? "Dhan rate limit reached; the last verified snapshot is retained as stale." : "Dhan option-chain data is unavailable.");
      const result = await response.json() as Chain;
      if (!signal?.aborted) setChain(result.available ? result : { ...unavailable(result.message ?? "No provider-confirmed chain is available for this expiry."), underlying, expiry });
    } catch (error) {
      if (signal?.aborted) return;
      const message = error instanceof Error ? error.message : "Dhan option-chain data is unavailable.";
      setChain((previous) => previous?.available
        ? { ...previous, status: "DATA STALE", stale: true, provider_error: message }
        : { ...unavailable(message), underlying, expiry });
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [underlying, expiry, apiBase, accessToken]);

  useEffect(() => {
    if (!underlying || !expiry) return;
    const controller = new AbortController();
    void loadChain(controller.signal);
    const timer = window.setInterval(() => void loadChain(controller.signal), 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [loadChain, refreshKey]);

  const stale = chain?.status === "DATA STALE" || chain?.stale;
  const visibleStatus = chain?.available ? (stale ? "STALE" : "LIVE") : chain?.status ?? (loading || expiryLoading ? "LOADING" : "DATA UNAVAILABLE");
  return <section id="options" className="option-chain-section" aria-labelledby="option-chain-heading">
    <div className="oc-heading-row">
      <div><p className="eyebrow">DHANHQ · NSE OPTIONS</p><h2 id="option-chain-heading">Option chain</h2><p className="oc-subtitle">Provider-confirmed contracts, source fields only</p></div>
      <span className={`oc-status ${stale ? "is-stale" : chain?.available ? "is-recent" : ""}`}><i />{visibleStatus}</span>
    </div>
    <div className="oc-controls">
      <label>Underlying<select value={underlying} onChange={(event) => { setUnderlying(event.target.value); setExpiry(""); setChain(null); }} disabled={!underlyings.length}>
        {underlyings.length ? underlyings.map((item) => <option key={item.symbol} value={item.symbol}>{item.symbol} · {item.exchange_segment}</option>) : <option value="">No eligible Dhan options</option>}
      </select></label>
      <label>Expiry<select value={expiry} onChange={(event) => { setExpiry(event.target.value); setChain(null); }} disabled={!expiries.length || expiryLoading}>
        {expiries.length ? expiries.map((item) => <option key={item} value={item}>{item}</option>) : <option value="">Unavailable</option>}
      </select></label>
      <button type="button" className="oc-refresh" disabled={!underlying || !expiry || loading} onClick={() => setRefreshKey((value) => value + 1)}>{loading ? "Refreshing…" : "Refresh chain"}</button>
      <div className="oc-spot"><span>Underlying spot</span><strong>{number(chain?.underlying_spot)}</strong></div>
    </div>
    {chain?.available && <div className={`oc-provenance ${stale ? "is-stale" : ""}`} role="status" aria-live="polite">
      <span><b>{stale ? "STALE" : "LIVE"}</b>{chain.cache_hit ? " · shared cached Dhan response" : " · Dhan response received"}</span>
      <span>Received {timeLabel(chain.source_timestamp)} IST</span>
      <span>{chain.provider_timestamp ? `Provider time ${timeLabel(chain.provider_timestamp)} IST` : "Dhan v2 does not supply a source event timestamp for this response."}</span>
      {chain.provider_error && <span>{chain.provider_error}</span>}
    </div>}
    {!chain?.available && <div className="oc-empty" role="status">{chain?.message ?? (underlyings.length ? "Choose an underlying and a Dhan-confirmed expiry." : "No eligible option instruments are currently synchronized from Dhan.")}</div>}
    {chain?.available && chain.strikes.length > 0 && <>
      <div className="oc-table-wrap"><table className="oc-table"><thead><tr><th scope="col">CALL</th><th scope="col">STRIKE</th><th scope="col">PUT</th></tr></thead>
        <tbody>{chain.strikes.map((row) => <tr key={row.strike}><td><ContractCell contract={row.call} /></td><th scope="row" className="oc-strike">{number(row.strike, 2)}</th><td><ContractCell contract={row.put} /></td></tr>)}</tbody>
      </table></div>
      <div className="oc-mobile-cards">{chain.strikes.map((row) => <article className="oc-mobile-row" key={row.strike}>
        <div className="oc-mobile-grid"><div className="oc-mobile-side"><b className="oc-mobile-label">CALL</b><ContractCell contract={row.call} /></div>
          <div className="oc-mobile-strike"><b className="oc-mobile-label">STRIKE</b><strong>{number(row.strike, 2)}</strong></div>
          <div className="oc-mobile-side"><b className="oc-mobile-label">PUT</b><ContractCell contract={row.put} /></div></div>
      </article>)}</div>
    </>}
    {chain?.available && chain.strikes.length === 0 && <div className="oc-empty" role="status">Dhan returned no strike rows for this expiry.</div>}
    <p className="oc-footnote">Missing Dhan fields are shown as unavailable. Premium and OI changes are calculated only when the matching current and previous values are both supplied.</p>
  </section>;
}
