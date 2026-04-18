"use client";

import { useEffect, useRef, useState, CSSProperties } from "react";

type SignalKind = "good" | "miss";
type TranscriptLine = {
  t: number;
  who: "ai" | "user";
  text: string;
  signal?: { kind: SignalKind; code: string; label: string };
};
type Signal = { code: string; label: string; score: number; note: string };
type Scenario = {
  id: string;
  title: string;
  role: string;
  customer: string;
  company: string;
  duration: number;
  transcript: TranscriptLine[];
  signals: Signal[];
  summary: string;
};

const SCENARIOS: Record<string, Scenario> = {
  pricing: {
    id: "pricing",
    title: "Pricing objection",
    role: "Dispatcher · inbound",
    customer: "Margaret Chen",
    company: "Westside Logistics",
    duration: 184,
    transcript: [
      { t: 0.5, who: "ai", text: "Hi, this is Margaret from Westside Logistics. I got the quote you sent — $840 a month. That's way more than I was expecting." },
      { t: 5.2, who: "user", text: "Thanks for calling, Margaret. Can you walk me through what you compared it against?" },
      { t: 9.0, who: "ai", text: "Your competitor came back at $540. I don't see what we're paying extra for." },
      { t: 13.0, who: "user", text: "That's a fair question. Our pricing includes 24/7 dispatch coverage and same-day response. What was included in their quote?", signal: { kind: "good", code: "PRC", label: "Asked qualifying question before defending price" } },
      { t: 18.4, who: "ai", text: "Honestly? I don't know. But I need to make a decision this week." },
      { t: 22.0, who: "user", text: "Okay. So $840 a month — yeah, I could probably knock a hundred off if that helps?", signal: { kind: "miss", code: "PRC", label: "Discounted before establishing value" } },
      { t: 26.5, who: "ai", text: "That's still more. What am I actually getting for the extra $200?" },
      { t: 30.0, who: "user", text: "Uh… we're more reliable. And, you know, our team's good.", signal: { kind: "miss", code: "CLR", label: "Vague value articulation under pressure" } },
      { t: 33.5, who: "ai", text: "Right. I think I'll go with the other option. Thanks." },
    ],
    signals: [
      { code: "CLR", label: "Clarity",    score: 5.8, note: "Struggled to articulate concrete value in the pricing moment." },
      { code: "CMP", label: "Composure",  score: 7.1, note: "Stayed calm; tone steady throughout objection." },
      { code: "PRC", label: "Process",    score: 4.2, note: "Discounted before qualifying. Skipped discovery step." },
      { code: "REC", label: "Recovery",   score: 3.5, note: "No attempt to re-engage after losing momentum." },
    ],
    summary: "Strong composure, weak process. Candidate defaulted to discount instead of running the value conversation.",
  },
  emergency: {
    id: "emergency",
    title: "Emergency intake",
    role: "Dispatcher · 24/7 line",
    customer: "David Alvarez",
    company: "Fleet Services",
    duration: 147,
    transcript: [
      { t: 0.5, who: "ai", text: "I've got a truck down on I-95, driver says there's smoke from the engine bay. What do I do?" },
      { t: 4.8, who: "user", text: "Okay, first — is the driver safely out of the vehicle? And clear of traffic?", signal: { kind: "good", code: "PRC", label: "Led with safety per intake protocol" } },
      { t: 9.0, who: "ai", text: "He's on the shoulder, yeah. But the truck is still on the road." },
      { t: 12.4, who: "user", text: "Got it. I'm dispatching fire and a tow to your mile marker now. What mile are you at, and is the engine off?", signal: { kind: "good", code: "CLR", label: "Parallel action — dispatch + info capture" } },
      { t: 17.2, who: "ai", text: "Mile 187 northbound. Engine's off." },
      { t: 20.0, who: "user", text: "Perfect. Stay with the driver on the line. I'll ping you when the tow is 5 minutes out." },
      { t: 24.0, who: "ai", text: "Thanks. One more thing — the load is refrigerated pharmaceuticals." },
      { t: 27.5, who: "user", text: "Understood, I'll flag that and loop in the client's ops lead in parallel.", signal: { kind: "good", code: "REC", label: "Captured load context + escalated proactively" } },
    ],
    signals: [
      { code: "CLR", label: "Clarity",    score: 9.1, note: "Clean, directive language. No filler under pressure." },
      { code: "CMP", label: "Composure",  score: 9.4, note: "Steady throughout. No voice escalation." },
      { code: "PRC", label: "Process",    score: 8.8, note: "Followed intake protocol in correct sequence." },
      { code: "REC", label: "Recovery",   score: 8.3, note: "Caught the pharma detail and escalated appropriately." },
    ],
    summary: "Textbook execution. Safety first, parallel dispatch, context capture. Candidate is ready for live line.",
  },
  cancellation: {
    id: "cancellation",
    title: "Cancellation save",
    role: "Sales · retention",
    customer: "Priya Shah",
    company: "Bloom & Co",
    duration: 198,
    transcript: [
      { t: 0.5, who: "ai", text: "I want to cancel. We're just not using it enough to justify the cost." },
      { t: 4.0, who: "user", text: "Got it. Before we process that — can I ask what 'not using enough' looks like for your team right now?", signal: { kind: "good", code: "PRC", label: "Diagnostic question before save attempt" } },
      { t: 9.0, who: "ai", text: "Honestly, only two of us log in. The rest of the team never touched it." },
      { t: 13.0, who: "user", text: "That tracks. Most teams hit adoption issues in month two. Would it be useful if I set up a 20-minute onboarding for the rest of the team this week?" },
      { t: 19.0, who: "ai", text: "Maybe. But I've already promised my CFO we'd cut this line item." },
      { t: 23.0, who: "user", text: "Completely fair. What if we paused the account for 60 days — no charge — and revisited once the team's actually onboarded?", signal: { kind: "good", code: "REC", label: "Offered structured alternative, not a discount" } },
      { t: 29.0, who: "ai", text: "That could work. Send me the details." },
    ],
    signals: [
      { code: "CLR", label: "Clarity",    score: 8.2, note: "Clear, unhurried language. Didn't rush the save." },
      { code: "CMP", label: "Composure",  score: 8.6, note: "Held steady when customer invoked CFO." },
      { code: "PRC", label: "Process",    score: 7.9, note: "Diagnosed before pitching. One weak handoff midway." },
      { code: "REC", label: "Recovery",   score: 8.8, note: "Pivoted to pause offer instead of discount. Strong." },
    ],
    summary: "Solid retention work. Candidate resisted discount reflex and offered a structured pause.",
  },
};

function IconPlay({ size = 14 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 14 14" fill="currentColor">
      <path d="M3.5 2v10l8.5-5z" />
    </svg>
  );
}
function IconPause({ size = 14 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 14 14" fill="currentColor">
      <rect x="3" y="2" width="3" height="10" rx="0.5" />
      <rect x="8" y="2" width="3" height="10" rx="0.5" />
    </svg>
  );
}
function IconRestart({ size = 14 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 7a5 5 0 1 1-1.5-3.5" />
      <path d="M12 2v3h-3" />
    </svg>
  );
}

function formatT(s: number) {
  const m = Math.floor(s / 60);
  const ss = Math.floor(s % 60);
  return `${m}:${String(ss).padStart(2, "0")}`;
}

function Waveform({ playing, speaking }: { playing: boolean; speaking: boolean }) {
  const [phase, setPhase] = useState(0);
  useEffect(() => {
    let raf: number;
    const tick = () => {
      setPhase((p) => p + (playing ? 0.18 : 0.04));
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing]);

  const N = 48;
  const bars: number[] = [];
  for (let i = 0; i < N; i++) {
    const activity = speaking ? 1 : 0.25;
    const env = Math.abs(Math.sin((i / N) * Math.PI));
    const micro = 0.5 + 0.5 * Math.sin(i * 0.9 + phase);
    const h = Math.max(6, env * micro * activity * 100);
    bars.push(h);
  }

  return (
    <div style={S.waveWrap}>
      <div style={S.waveBars}>
        {bars.map((h, i) => (
          <div
            key={i}
            style={{
              ...S.waveBar,
              height: `${h}%`,
              background: speaking ? "var(--accent)" : "var(--line-2)",
            }}
          />
        ))}
      </div>
    </div>
  );
}

function Tabs({ current, onChange }: { current: string; onChange: (id: string) => void }) {
  return (
    <div style={S.tabs}>
      {Object.values(SCENARIOS).map((s) => {
        const on = current === s.id;
        return (
          <button
            key={s.id}
            onClick={() => onChange(s.id)}
            style={{
              ...S.tab,
              color: on ? "var(--ink)" : "var(--ink-3)",
              borderBottom: on ? "2px solid var(--accent)" : "2px solid transparent",
            }}
          >
            <span style={S.tabTitle}>{s.title}</span>
            <span style={S.tabRole}>{s.role}</span>
          </button>
        );
      })}
    </div>
  );
}

function TranscriptLineRow({ line, visible }: { line: TranscriptLine; visible: boolean }) {
  if (!visible) return null;
  const isAI = line.who === "ai";
  return (
    <div style={{ ...S.line, opacity: 1, transform: "translateY(0)", transition: "opacity .4s ease, transform .4s ease" }}>
      <div style={{ ...S.bubbleSide, alignItems: isAI ? "flex-start" : "flex-end" }}>
        <div style={S.lineMeta}>
          <span style={{ ...S.lineWho, color: isAI ? "var(--accent-ink)" : "var(--ink-2)" }}>
            {isAI ? "AI CUSTOMER" : "DISPATCHER"}
          </span>
          <span style={S.lineT}>{formatT(line.t)}</span>
        </div>
        <div
          style={{
            ...S.bubble,
            background: isAI ? "var(--accent-soft)" : "var(--bg-2)",
            color: "var(--ink)",
            borderTopLeftRadius: isAI ? 4 : 14,
            borderTopRightRadius: isAI ? 14 : 4,
          }}
        >
          {line.text}
        </div>
        {line.signal && (
          <div
            style={{
              ...S.inlineSignal,
              color: line.signal.kind === "good" ? "var(--signal-good)" : "var(--signal-miss)",
            }}
          >
            <span
              style={{
                ...S.sigDot,
                background: line.signal.kind === "good" ? "var(--signal-good)" : "var(--signal-miss)",
              }}
            />
            <span style={S.sigCode}>[{line.signal.code}]</span>
            <span style={S.sigText}>{line.signal.label}</span>
          </div>
        )}
      </div>
    </div>
  );
}

function SignalCard({ sig, revealed, index }: { sig: Signal; revealed: boolean; index: number }) {
  const target = revealed ? sig.score : 0;
  const [display, setDisplay] = useState(0);
  useEffect(() => {
    if (!revealed) {
      setDisplay(0);
      return;
    }
    let raf: number;
    let v = 0;
    const step = () => {
      v += (target - v) * 0.12;
      if (Math.abs(target - v) < 0.02) v = target;
      setDisplay(v);
      if (v < target) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [revealed, target]);

  const pct = (display / 10) * 100;
  const color =
    display >= 7.5 ? "var(--signal-good)" : display >= 5.5 ? "var(--signal-warn)" : "var(--signal-miss)";

  return (
    <div
      style={{
        ...S.signalCard,
        opacity: revealed ? 1 : 0.35,
        transition: "opacity .4s ease",
        transitionDelay: `${index * 80}ms`,
      }}
    >
      <div style={S.sigTop}>
        <div style={S.sigLabel}>
          <span style={S.sigLabelCode}>{sig.code}</span>
          <span style={S.sigLabelName}>{sig.label}</span>
        </div>
        <div style={{ ...S.sigScore, color }}>
          {display.toFixed(1)}
          <span style={S.sigOutOf}>/10</span>
        </div>
      </div>
      <div style={S.sigBarWrap}>
        <div style={{ ...S.sigBarFill, width: `${pct}%`, background: color }} />
      </div>
      <div style={S.sigNote}>{sig.note}</div>
    </div>
  );
}

export default function LandingDemo() {
  const [scenarioId, setScenarioId] = useState<string>("pricing");
  const scenario = SCENARIOS[scenarioId];
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const endRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const maxT = scenario.transcript[scenario.transcript.length - 1].t + 4;

  useEffect(() => {
    setTime(0);
    setPlaying(false);
  }, [scenarioId]);

  useEffect(() => {
    if (!playing) return;
    let last = performance.now();
    let raf: number;
    const step = (now: number) => {
      const dt = (now - last) / 1000;
      last = now;
      setTime((t) => {
        const next = t + dt * 1.6;
        if (next >= maxT) {
          setPlaying(false);
          return maxT;
        }
        return next;
      });
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [playing, maxT]);

  useEffect(() => {
    if (scrollRef.current) {
      const el = scrollRef.current;
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    }
  }, [time]);

  const visibleLines = scenario.transcript.filter((l) => l.t <= time);
  const done = time >= maxT - 0.05;
  const progress = Math.min(1, time / maxT);

  let speaking = false;
  let speaker: "ai" | "user" | null = null;
  for (let i = scenario.transcript.length - 1; i >= 0; i--) {
    const l = scenario.transcript[i];
    if (l.t <= time && time - l.t < 3.2) {
      speaking = playing;
      speaker = l.who;
      break;
    }
  }

  const signalsRevealed = done;

  return (
    <div style={S.root}>
      <div style={S.header}>
        <Tabs current={scenarioId} onChange={setScenarioId} />
      </div>

      <div style={S.body} className="biq-demo-body">
        <div style={S.leftCol}>
          <div style={S.callPanel}>
            <div style={S.callHead}>
              <div style={S.callWho}>
                <div style={S.avatar}>
                  {scenario.customer.split(" ").map((s) => s[0]).join("")}
                </div>
                <div>
                  <div style={S.callName}>{scenario.customer}</div>
                  <div style={S.callCo}>{scenario.company}</div>
                </div>
              </div>
              <div style={{ ...S.liveBadge, opacity: playing ? 1 : 0.35 }}>
                <span style={S.liveDot} />
                {done ? "ENDED" : playing ? "LIVE" : "READY"}
              </div>
            </div>

            <Waveform playing={playing} speaking={speaker === "ai" && speaking} />

            <div style={S.callMeta}>
              <div style={S.callMetaItem}>
                <span style={S.metaK}>SCENARIO</span>
                <span style={S.metaV}>{scenario.title}</span>
              </div>
              <div style={S.callMetaItem}>
                <span style={S.metaK}>VOICE</span>
                <span style={S.metaV}>ElevenLabs</span>
              </div>
              <div style={S.callMetaItem}>
                <span style={S.metaK}>ELAPSED</span>
                <span style={S.metaV}>{formatT(time)}</span>
              </div>
            </div>

            <div style={S.controls}>
              <button
                style={{
                  ...S.playBtn,
                  background: playing ? "var(--bg-2)" : "var(--ink)",
                  color: playing ? "var(--ink)" : "var(--bg)",
                }}
                onClick={() => {
                  if (done) {
                    setTime(0);
                    setPlaying(true);
                    return;
                  }
                  setPlaying((p) => !p);
                }}
              >
                {done ? <IconRestart /> : playing ? <IconPause /> : <IconPlay />}
                <span>{done ? "Replay" : playing ? "Pause" : time > 0 ? "Resume" : "Play scenario"}</span>
              </button>
              <button
                style={S.resetBtn}
                onClick={() => {
                  setTime(0);
                  setPlaying(false);
                }}
                title="Reset"
              >
                <IconRestart />
              </button>
            </div>

            <div style={S.progressWrap}>
              <div style={{ ...S.progressFill, width: `${progress * 100}%` }} />
            </div>
          </div>
        </div>

        <div style={S.midCol}>
          <div style={S.colHead}>
            <span style={S.colHeadK}>TRANSCRIPT</span>
            <span style={S.colHeadV}>
              {visibleLines.length} / {scenario.transcript.length}
            </span>
          </div>
          <div style={S.transcript} ref={scrollRef}>
            {scenario.transcript.map((line, i) => (
              <TranscriptLineRow key={i} line={line} visible={line.t <= time} />
            ))}
            <div ref={endRef} />
            {visibleLines.length === 0 && (
              <div style={S.emptyTranscript}>
                Press play to start the scenario. The AI customer will call in.
              </div>
            )}
          </div>
        </div>

        <div style={S.rightCol}>
          <div style={S.colHead}>
            <span style={S.colHeadK}>BEHAVIORAL SIGNALS</span>
            <span
              style={{
                ...S.colHeadV,
                color: signalsRevealed ? "var(--accent-ink)" : "var(--ink-3)",
              }}
            >
              {signalsRevealed ? "COMPLETE" : "PROCESSING…"}
            </span>
          </div>
          <div style={S.signals}>
            {scenario.signals.map((sig, i) => (
              <SignalCard key={sig.code} sig={sig} revealed={signalsRevealed} index={i} />
            ))}
          </div>
          <div
            style={{
              ...S.summary,
              opacity: signalsRevealed ? 1 : 0,
              transition: "opacity .4s ease",
              transitionDelay: "400ms",
            }}
          >
            <div style={S.summaryK}>SESSION SUMMARY</div>
            <div style={S.summaryV}>{scenario.summary}</div>
          </div>
        </div>
      </div>

      <style>{`
        @media (max-width: 1080px) {
          .biq-demo-body {
            grid-template-columns: 1fr !important;
          }
          .biq-demo-body > div:first-child,
          .biq-demo-body > div:nth-child(2) {
            border-right: none !important;
            border-bottom: 1px solid var(--line) !important;
          }
        }
      `}</style>
    </div>
  );
}

const S: Record<string, CSSProperties> = {
  root: { fontFamily: "var(--font-sans), sans-serif", color: "var(--ink)" },
  header: { borderBottom: "1px solid var(--line)", background: "var(--bg)" },
  tabs: { display: "flex", gap: 4, padding: "0 20px", overflowX: "auto" },
  tab: {
    padding: "18px 20px 16px",
    display: "flex",
    flexDirection: "column",
    alignItems: "flex-start",
    gap: 4,
    cursor: "pointer",
    whiteSpace: "nowrap",
    transition: "color .15s ease, border-color .15s ease",
    background: "none",
    border: "none",
    borderBottom: "2px solid transparent",
  },
  tabTitle: { fontFamily: "var(--font-display), serif", fontSize: 20, letterSpacing: "-0.01em", fontWeight: 400 },
  tabRole: {
    fontFamily: "var(--font-mono), monospace",
    fontSize: 10.5,
    letterSpacing: "0.06em",
    color: "var(--ink-3)",
    textTransform: "uppercase",
  },
  body: { display: "grid", gridTemplateColumns: "320px 1fr 320px", minHeight: 560 },
  leftCol: { borderRight: "1px solid var(--line)", padding: 24, background: "var(--bg)" },
  callPanel: { display: "flex", flexDirection: "column", gap: 20 },
  callHead: { display: "flex", justifyContent: "space-between", alignItems: "center" },
  callWho: { display: "flex", gap: 12, alignItems: "center" },
  avatar: {
    width: 40,
    height: 40,
    borderRadius: "50%",
    background: "var(--accent-soft)",
    color: "var(--accent-ink)",
    display: "grid",
    placeItems: "center",
    fontFamily: "var(--font-mono), monospace",
    fontSize: 12,
    fontWeight: 600,
    letterSpacing: "0.05em",
  },
  callName: { fontSize: 14, fontWeight: 500, color: "var(--ink)" },
  callCo: { fontFamily: "var(--font-mono), monospace", fontSize: 11, color: "var(--ink-3)", letterSpacing: "0.04em" },
  liveBadge: {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    fontFamily: "var(--font-mono), monospace",
    fontSize: 10.5,
    letterSpacing: "0.1em",
    color: "var(--accent-ink)",
    transition: "opacity .3s ease",
  },
  liveDot: { width: 6, height: 6, borderRadius: "50%", background: "var(--accent)", animation: "biq-pulse 2s ease-in-out infinite" },
  waveWrap: { height: 120, display: "flex", alignItems: "center", padding: "0 4px" },
  waveBars: { display: "flex", alignItems: "center", gap: 3, height: "100%", width: "100%" },
  waveBar: { flex: 1, minHeight: 4, borderRadius: 1.5, transition: "height .12s ease-out, background .3s" },
  callMeta: {
    display: "flex",
    flexDirection: "column",
    gap: 10,
    padding: "16px 0",
    borderTop: "1px dashed var(--line-2)",
    borderBottom: "1px dashed var(--line-2)",
  },
  callMetaItem: { display: "flex", justifyContent: "space-between", alignItems: "center" },
  metaK: { fontFamily: "var(--font-mono), monospace", fontSize: 10.5, color: "var(--ink-3)", letterSpacing: "0.06em" },
  metaV: { fontFamily: "var(--font-mono), monospace", fontSize: 12, color: "var(--ink-2)" },
  controls: { display: "flex", gap: 8 },
  playBtn: {
    flex: 1,
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    padding: "12px 16px",
    borderRadius: 10,
    fontSize: 13.5,
    fontWeight: 500,
    transition: "background .15s ease",
    border: "none",
    cursor: "pointer",
  },
  resetBtn: {
    width: 44,
    display: "grid",
    placeItems: "center",
    borderRadius: 10,
    background: "var(--bg-2)",
    color: "var(--ink-2)",
    border: "1px solid var(--line)",
    cursor: "pointer",
  },
  progressWrap: { height: 2, background: "var(--line)", borderRadius: 1, overflow: "hidden" },
  progressFill: { height: "100%", background: "var(--accent)", transition: "width .15s linear" },

  midCol: { borderRight: "1px solid var(--line)", display: "flex", flexDirection: "column" },
  colHead: {
    padding: "18px 24px",
    borderBottom: "1px solid var(--line)",
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    fontFamily: "var(--font-mono), monospace",
    fontSize: 11,
    letterSpacing: "0.1em",
  },
  colHeadK: { color: "var(--ink-3)", textTransform: "uppercase" },
  colHeadV: { color: "var(--ink-2)", textTransform: "uppercase" },
  transcript: { flex: 1, padding: "24px 28px", overflowY: "auto", maxHeight: 520, display: "flex", flexDirection: "column", gap: 18 },
  emptyTranscript: { color: "var(--ink-3)", fontSize: 14, fontStyle: "italic", paddingTop: 40, textAlign: "center" },
  line: { display: "flex", width: "100%" },
  bubbleSide: { display: "flex", flexDirection: "column", gap: 6, maxWidth: "88%", width: "100%" },
  lineMeta: { display: "flex", gap: 10, alignItems: "center" },
  lineWho: { fontFamily: "var(--font-mono), monospace", fontSize: 10.5, letterSpacing: "0.08em", fontWeight: 600 },
  lineT: { fontFamily: "var(--font-mono), monospace", fontSize: 10.5, color: "var(--ink-3)" },
  bubble: { padding: "12px 16px", borderRadius: 14, fontSize: 14.5, lineHeight: 1.5 },
  inlineSignal: {
    display: "inline-flex",
    alignItems: "center",
    gap: 8,
    padding: "6px 0",
    fontFamily: "var(--font-mono), monospace",
    fontSize: 11,
    letterSpacing: "0.03em",
  },
  sigDot: { width: 6, height: 6, borderRadius: "50%", flexShrink: 0 },
  sigCode: { fontWeight: 600 },
  sigText: { color: "var(--ink-2)" },

  rightCol: { display: "flex", flexDirection: "column", background: "var(--bg)" },
  signals: { padding: 20, display: "flex", flexDirection: "column", gap: 14 },
  signalCard: { padding: "14px 16px", border: "1px solid var(--line)", borderRadius: 10, background: "var(--paper)" },
  sigTop: { display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 10 },
  sigLabel: { display: "flex", gap: 8, alignItems: "baseline" },
  sigLabelCode: { fontFamily: "var(--font-mono), monospace", fontSize: 10.5, color: "var(--ink-3)", letterSpacing: "0.08em" },
  sigLabelName: { fontSize: 13.5, fontWeight: 500, color: "var(--ink)" },
  sigScore: { fontFamily: "var(--font-display), serif", fontSize: 24, letterSpacing: "-0.01em" },
  sigOutOf: { fontFamily: "var(--font-mono), monospace", fontSize: 10.5, color: "var(--ink-3)", marginLeft: 2 },
  sigBarWrap: { height: 3, background: "var(--line)", borderRadius: 2, overflow: "hidden", marginBottom: 8 },
  sigBarFill: { height: "100%", borderRadius: 2, transition: "width .6s ease-out" },
  sigNote: { fontSize: 12, color: "var(--ink-3)", lineHeight: 1.45 },
  summary: {
    margin: "4px 20px 20px",
    padding: "16px 18px",
    background: "var(--accent-soft)",
    borderRadius: 10,
    border: "1px solid color-mix(in oklab, var(--accent) 25%, transparent)",
  },
  summaryK: { fontFamily: "var(--font-mono), monospace", fontSize: 10.5, color: "var(--accent-ink)", letterSpacing: "0.08em", marginBottom: 6 },
  summaryV: { fontSize: 13.5, color: "var(--ink)", lineHeight: 1.5 },
};
