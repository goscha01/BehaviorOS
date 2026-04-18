"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import "./landing.css";
import LandingDemo from "@/components/landing/LandingDemo";
import EarlyAccessModal from "@/components/landing/EarlyAccessModal";

export default function LandingPage() {
  const heroBarsRef = useRef<HTMLDivElement>(null);
  const howBarsRef = useRef<HTMLDivElement>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const openModal = () => setModalOpen(true);
  const closeModal = () => setModalOpen(false);

  useEffect(() => {
    const heroEl = heroBarsRef.current;
    const howEl = howBarsRef.current;

    const makeBars = (el: HTMLDivElement | null, n: number) => {
      if (!el) return;
      el.innerHTML = "";
      for (let i = 0; i < n; i++) {
        const b = document.createElement("div");
        b.className = "bar";
        el.appendChild(b);
      }
    };

    makeBars(heroEl, 60);
    makeBars(howEl, 40);

    let t = 0;
    let raf = 0;
    const tick = () => {
      t += 1;
      if (heroEl) {
        const bars = heroEl.children;
        for (let i = 0; i < bars.length; i++) {
          const env = 0.35 + 0.55 * Math.abs(Math.sin((i / bars.length) * Math.PI + t * 0.02));
          const micro = 0.6 + 0.4 * Math.sin(i * 0.7 + t * 0.18 + Math.sin(t * 0.04) * 2);
          const h = Math.max(4, env * micro * 100);
          (bars[i] as HTMLElement).style.height = h + "%";
        }
      }
      if (howEl) {
        const bars = howEl.children;
        for (let i = 0; i < bars.length; i++) {
          const v = 0.2 + 0.8 * Math.abs(Math.sin(i * 0.5 + t * 0.12));
          (bars[i] as HTMLElement).style.height = v * 100 + "%";
        }
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  return (
    <div className="biq" data-accent="terracotta">
      <nav className="top">
        <div className="wrap">
          <div className="mark">
            <span className="glyph">BIQ</span>
            <span>BehavioralIQ</span>
          </div>
          <div className="links">
            <a href="#product">Product</a>
            <a href="#demo">Demo</a>
            <a href="#use-cases">Use cases</a>
            <a href="#start">Pricing</a>
          </div>
          <button type="button" onClick={openModal} className="cta">
            Start a session →
          </button>
        </div>
      </nav>

      <section className="hero">
        <div className="wrap">
          <div className="grid">
            <div>
              <div className="eyebrow">
                <span className="dot"></span> Human performance, measured
              </div>
              <h1 className="display">
                Turn real conversations into <em>measurable</em> human performance.
              </h1>
              <p className="lede">
                AI that reveals how people think, decide, and respond — across voice and chat.
                Simulate real scenarios, capture interactions, and turn them into structured signals
                you can act on.
              </p>
              <div className="for-line">
                <span>For:</span>
                <span className="chip">Hiring teams</span>
                <span className="chip">Sales leaders</span>
                <span className="chip">Service businesses</span>
              </div>
              <div className="btn-row">
                <button type="button" onClick={openModal} className="btn primary">
                  Start your first session <span className="arr">→</span>
                </button>
                <a href="#demo" className="btn ghost">
                  See live demo
                </a>
              </div>
              <div className="note">
                <span>No credit card</span>
                <span>Set up in under 5 minutes</span>
                <span>Voice + chat</span>
              </div>
            </div>
            <div>
              <div className="wave-card" id="hero-wave">
                <div className="meta">
                  <div>SESSION · DISPATCH-042</div>
                  <div className="live">Live</div>
                </div>
                <div className="wave-svg">
                  <div className="wave-bars" ref={heroBarsRef}></div>
                </div>
                <div className="readout">
                  <div className="r">
                    <span className="k">Clarity</span>
                    <span className="v">
                      8.4<small>/10</small>
                    </span>
                  </div>
                  <div className="r">
                    <span className="k">Composure</span>
                    <span className="v">7.9</span>
                  </div>
                  <div className="r">
                    <span className="k">Process</span>
                    <span className="v">6.2</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <div className="pain-wrap">
        <section>
          <div className="wrap">
            <div className="section-label">The problem</div>
            <h2 className="section">
              You don&apos;t actually know how your team performs —{" "}
              <em>until it&apos;s too late.</em>
            </h2>
            <p className="section-sub">
              Interviews, calls, and training all happen in different places. You&apos;re making
              decisions without real behavioral data.
            </p>

            <div className="pain-list">
              <div className="pain-item">
                <span className="num">01</span>
                <div className="txt">Interviews don&apos;t reflect real behavior</div>
              </div>
              <div className="pain-item">
                <span className="num">02</span>
                <div className="txt">Calls and chats are inconsistent and hard to evaluate</div>
              </div>
              <div className="pain-item">
                <span className="num">03</span>
                <div className="txt">Training is subjective and depends on managers</div>
              </div>
              <div className="pain-item">
                <span className="num">04</span>
                <div className="txt">Top performers are hard to identify and replicate</div>
              </div>
              <div className="pain-item">
                <span className="num">05</span>
                <div className="txt">Mistakes happen in real client interactions, not in training</div>
              </div>
            </div>
          </div>
        </section>
      </div>

      <section id="product">
        <div className="wrap">
          <div className="section-label">How it works</div>
          <h2 className="section">
            See how people actually perform — <em>before it impacts your business.</em>
          </h2>
          <p className="section-sub">
            Three steps. A scenario, a real interaction, and the structured signals that come out of
            it.
          </p>

          <div className="how">
            <div className="how-step">
              <span className="n">01 / CONFIGURE</span>
              <div className="title">Create a scenario</div>
              <div className="desc">
                Define your business context, script, and goals. Use a template or build from
                scratch.
              </div>
              <div className="viz">
                <div className="viz-scenario">
                  <span className="tag">context</span>
                  <span className="tag">script</span>
                  <span className="tag">goals</span>
                </div>
              </div>
            </div>
            <div className="how-step">
              <span className="n">02 / RUN</span>
              <div className="title">Run a real interaction</div>
              <div className="desc">
                AI acts as the customer via ElevenLabs voice. Your candidate or employee responds in
                real-time.
              </div>
              <div className="viz">
                <div className="viz-call" ref={howBarsRef}></div>
              </div>
            </div>
            <div className="how-step">
              <span className="n">03 / MEASURE</span>
              <div className="title">Get structured signals</div>
              <div className="desc">
                What they said. What they missed. How they handled pressure. Where they broke
                process.
              </div>
              <div className="viz">
                <div className="viz-signals">
                  <div className="row">
                    <span>CLR</span>
                    <div className="bar">
                      <span style={{ width: "84%" }}></span>
                    </div>
                    <span>8.4</span>
                  </div>
                  <div className="row">
                    <span>CMP</span>
                    <div className="bar">
                      <span style={{ width: "79%" }}></span>
                    </div>
                    <span>7.9</span>
                  </div>
                  <div className="row">
                    <span>PRC</span>
                    <div className="bar">
                      <span style={{ width: "62%" }}></span>
                    </div>
                    <span>6.2</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section id="demo" style={{ paddingTop: 24 }}>
        <div className="wrap">
          <div className="section-label">Interactive demo</div>
          <h2 className="section">
            A pricing objection. <em>In real time.</em>
          </h2>
          <p className="section-sub">
            An AI customer calls your dispatcher. Press play — the transcript streams, the waveform
            moves, and behavioral signals populate as they&apos;d appear after a real session.
          </p>
          <div id="demo-root">
            <LandingDemo />
          </div>
        </div>
      </section>

      <section className="tight">
        <div className="wrap">
          <div className="section-label">What you get</div>
          <h2 className="section">
            Structured signals. <em>Not vague feedback.</em>
          </h2>

          <div className="what-grid">
            <div className="item">
              <span className="num">01</span>
              <div className="label">Scenario-based simulations</div>
              <div className="desc">
                Not fake interviews. Real situations your team actually handles.
              </div>
            </div>
            <div className="item">
              <span className="num">02</span>
              <div className="label">Voice + chat analysis</div>
              <div className="desc">One system, both modalities, consistent signals across them.</div>
            </div>
            <div className="item">
              <span className="num">03</span>
              <div className="label">Behavioral signals</div>
              <div className="desc">
                Structured scoring on clarity, composure, process, and recovery.
              </div>
            </div>
            <div className="item">
              <span className="num">04</span>
              <div className="label">Repeatable training</div>
              <div className="desc">
                The same scenario. Every candidate. Every new hire. Every quarter.
              </div>
            </div>
            <div className="item">
              <span className="num">05</span>
              <div className="label">Performance over time</div>
              <div className="desc">Track individual and team trajectories. See who&apos;s improving.</div>
            </div>
          </div>
        </div>
      </section>

      <div className="uc-wrap" id="use-cases">
        <section>
          <div className="wrap">
            <div className="section-label">Use cases</div>
            <h2 className="section">
              One system. <em>Three teams that rely on human performance.</em>
            </h2>

            <div className="uc">
              <div className="uc-card">
                <span className="kind">Hiring</span>
                <h3>Test candidates in real situations before hiring.</h3>
                <p>
                  Stop hiring on gut feel. See how a candidate handles the exact calls they&apos;ll
                  be taking on day one.
                </p>
                <div className="ex">EX — &ldquo;Angry customer threatens to cancel&rdquo;</div>
              </div>
              <div className="uc-card">
                <span className="kind">Sales</span>
                <h3>Understand how leads are handled and where deals are lost.</h3>
                <p>
                  Every rep, every objection, every call pattern. Replicate what top performers do,
                  fix what breaks.
                </p>
                <div className="ex">EX — &ldquo;Pricing objection on discovery call&rdquo;</div>
              </div>
              <div className="uc-card">
                <span className="kind">Ops / Dispatch</span>
                <h3>Ensure consistency, compliance, and quality in real interactions.</h3>
                <p>
                  Audit the conversations that already happen. Coach against a standard, not a
                  manager&apos;s memory.
                </p>
                <div className="ex">EX — &ldquo;Missed emergency intake protocol&rdquo;</div>
              </div>
            </div>
          </div>
        </section>
      </div>

      <section className="offer" id="start">
        <div className="inner">
          <div className="section-label" style={{ justifyContent: "center" }}>
            Start free
          </div>
          <h2>
            Start seeing real performance — <em>not assumptions.</em>
          </h2>
          <p className="lede">
            Run your first training scenario in minutes. No setup complexity. Works with your
            existing workflows.
          </p>
          <div className="btn-row">
            <button type="button" onClick={openModal} className="btn primary">
              Start your first session <span className="arr">→</span>
            </button>
            <a href="#demo" className="btn ghost">
              See live demo
            </a>
          </div>

          <div className="signup-list">
            <div className="li">
              <span className="k">01</span>
              <span>Pre-built dispatcher training scenarios</span>
            </div>
            <div className="li">
              <span className="k">02</span>
              <span>AI voice interactions powered by ElevenLabs</span>
            </div>
            <div className="li">
              <span className="k">03</span>
              <span>Full session transcripts + behavioral signals</span>
            </div>
            <div className="li">
              <span className="k">04</span>
              <span>Templates for hiring and training, ready to go</span>
            </div>
          </div>

          <div className="note" style={{ justifyContent: "center", marginTop: 32 }}>
            <span>No credit card required</span>
            <span>Set up in under 5 minutes</span>
          </div>
        </div>
      </section>

      <footer>
        <div className="wrap">
          <div className="mark" style={{ fontSize: 18 }}>
            <span className="glyph" style={{ width: 24, height: 24, fontSize: 9 }}>
              BIQ
            </span>
            <span>BehavioralIQ</span>
          </div>
          <div>
            © 2026 BehavioralIQ · <Link href="/login">Login</Link> ·{" "}
            <button
              type="button"
              onClick={openModal}
              style={{ color: "inherit", padding: 0, font: "inherit", cursor: "pointer" }}
            >
              Request access
            </button>
          </div>
        </div>
      </footer>

      <EarlyAccessModal open={modalOpen} onClose={closeModal} />
    </div>
  );
}
