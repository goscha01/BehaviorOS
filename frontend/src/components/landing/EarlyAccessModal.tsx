"use client";

import { useEffect, useState, FormEvent } from "react";
import emailjs from "@emailjs/browser";

const EMAILJS_PUBLIC_KEY = "RrCIuTQfiMgOC8e9N";
const EMAILJS_SERVICE_ID = "service_3krrjqe";
const EMAILJS_TEMPLATE_ID = "template_ytpnidr";

let initialized = false;

export default function EarlyAccessModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [success, setSuccess] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!initialized) {
      emailjs.init(EMAILJS_PUBLIC_KEY);
      initialized = true;
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = "";
      document.removeEventListener("keydown", onKey);
    };
  }, [open, onClose]);

  useEffect(() => {
    if (open) {
      setSuccess(false);
      setError(null);
    }
  }, [open]);

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const name = (form.elements.namedItem("name") as HTMLInputElement).value;
    const company = (form.elements.namedItem("company") as HTMLInputElement).value;
    const email = (form.elements.namedItem("email") as HTMLInputElement).value;
    const size = (form.elements.namedItem("size") as HTMLSelectElement).value;
    const useCase = (form.elements.namedItem("useCase") as HTMLSelectElement).value;

    setSubmitting(true);
    setError(null);

    const message =
      "BehavioralIQ — Early access request\n\n" +
      "Company: " + company + "\n" +
      "Primary use case: " + useCase + "\n" +
      "Team size: " + size + "\n" +
      "Contact: " + name + " <" + email + ">";

    try {
      await emailjs.send(EMAILJS_SERVICE_ID, EMAILJS_TEMPLATE_ID, {
        name,
        email,
        message,
      });
      setSuccess(true);
    } catch (err) {
      console.error("EmailJS error:", err);
      setError("Something went wrong. Email us directly at info@geos-ai.com.");
    } finally {
      setSubmitting(false);
    }
  }

  if (!open) return null;

  return (
    <div
      className="biq-modal-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="biq-modal" role="dialog" aria-modal="true" aria-labelledby="biq-modal-title">
        <button className="biq-modal-close" onClick={onClose} aria-label="Close">
          ×
        </button>

        {success ? (
          <div className="biq-modal-success">
            <div className="biq-check-ok">✓</div>
            <h3 id="biq-modal-title">You&apos;re on the list.</h3>
            <p>
              We&apos;ll be in touch within 2 business days with next steps and early-access
              details.
            </p>
            <button className="btn ghost" style={{ marginTop: 24 }} onClick={onClose}>
              Close
            </button>
          </div>
        ) : (
          <>
            <span className="biq-modal-eyebrow">Early access</span>
            <h3 id="biq-modal-title">Run your first scenario with us.</h3>
            <p>
              We&apos;re onboarding a small batch of hiring, sales, and ops teams. Tell us about
              yours.
            </p>
            <form onSubmit={handleSubmit} className="biq-modal-form">
              <div className="biq-field">
                <label htmlFor="biq-name">Your name</label>
                <input id="biq-name" name="name" type="text" required placeholder="Jane Smith" />
              </div>
              <div className="biq-field">
                <label htmlFor="biq-company">Company</label>
                <input
                  id="biq-company"
                  name="company"
                  type="text"
                  required
                  placeholder="Acme Dispatch Co."
                />
              </div>
              <div className="biq-field">
                <label htmlFor="biq-email">Work email</label>
                <input
                  id="biq-email"
                  name="email"
                  type="email"
                  required
                  placeholder="jane@acme.com"
                />
              </div>
              <div className="biq-field">
                <label htmlFor="biq-size">Team size</label>
                <select id="biq-size" name="size" required defaultValue="">
                  <option value="" disabled>
                    Select…
                  </option>
                  <option>1–5</option>
                  <option>6–25</option>
                  <option>26–100</option>
                  <option>101–500</option>
                  <option>500+</option>
                </select>
              </div>
              <div className="biq-field">
                <label htmlFor="biq-use-case">Primary use case</label>
                <select id="biq-use-case" name="useCase" required defaultValue="">
                  <option value="" disabled>
                    Select…
                  </option>
                  <option>Hiring — screening candidates</option>
                  <option>Sales — rep training / call review</option>
                  <option>Ops / Dispatch — quality & compliance</option>
                  <option>General training / onboarding</option>
                  <option>Other</option>
                </select>
              </div>
              <button
                type="submit"
                disabled={submitting}
                className="btn primary biq-modal-submit"
              >
                {submitting ? "Sending…" : "Request early access →"}
              </button>
              {error && <p className="biq-modal-error">{error}</p>}
              <p className="biq-modal-fineprint">
                We&apos;ll reach out within 2 business days. No spam.
              </p>
            </form>
          </>
        )}
      </div>
    </div>
  );
}
