"use client";

import { useEffect, useState } from "react";
import { apiGet, apiPost } from "@/lib/api";
import type { Subscription } from "@/lib/types";

const PLANS = [
  {
    id: "starter",
    name: "Starter",
    price: "$49/mo",
    features: [
      "Up to 50 training sessions/month",
      "1 business profile",
      "Standard scenarios",
      "Email support",
    ],
  },
  {
    id: "pro",
    name: "Pro",
    price: "$149/mo",
    features: [
      "Unlimited training sessions",
      "Multiple business profiles",
      "Custom scenarios & rubrics",
      "Priority support",
      "Advanced analytics",
    ],
  },
];

export default function BillingPage() {
  const [subscription, setSubscription] = useState<Subscription | null>(null);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState<string | null>(null);

  useEffect(() => {
    apiGet<Subscription>("/api/billing/subscription/")
      .then(setSubscription)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const handleSubscribe = async (plan: string) => {
    setActionLoading(plan);
    try {
      const data = await apiPost<{ checkout_url: string }>("/api/billing/checkout/", {
        plan,
        success_url: `${window.location.origin}/billing?success=true`,
        cancel_url: `${window.location.origin}/billing`,
      });
      window.location.href = data.checkout_url;
    } catch (err) {
      alert("Failed to create checkout session");
    } finally {
      setActionLoading(null);
    }
  };

  const handleManage = async () => {
    setActionLoading("manage");
    try {
      const data = await apiPost<{ portal_url: string }>("/api/billing/portal/", {
        return_url: `${window.location.origin}/billing`,
      });
      window.location.href = data.portal_url;
    } catch {
      alert("Failed to open billing portal");
    } finally {
      setActionLoading(null);
    }
  };

  if (loading) {
    return <div className="animate-pulse h-64 bg-gray-100 rounded-lg" />;
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Billing</h1>
        <p className="mt-1 text-gray-500">Manage your subscription and billing</p>
      </div>

      {/* Current Plan */}
      {subscription?.plan && (
        <div className="bg-white rounded-lg shadow p-6">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-lg font-semibold text-gray-900">
                Current Plan: <span className="capitalize">{subscription.plan}</span>
              </h3>
              <p className="text-sm text-gray-500">
                Status:{" "}
                <span
                  className={
                    subscription.status === "active"
                      ? "text-green-600"
                      : "text-yellow-600"
                  }
                >
                  {subscription.status}
                </span>
              </p>
              {subscription.current_period_end && (
                <p className="text-sm text-gray-500">
                  Renews: {new Date(subscription.current_period_end).toLocaleDateString()}
                </p>
              )}
              {subscription.cancel_at_period_end && (
                <p className="text-sm text-red-500">Cancels at end of period</p>
              )}
            </div>
            <button
              onClick={handleManage}
              disabled={actionLoading === "manage"}
              className="px-4 py-2 border border-gray-300 rounded-md text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              {actionLoading === "manage" ? "Loading..." : "Manage Subscription"}
            </button>
          </div>
        </div>
      )}

      {/* Plans */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {PLANS.map((plan) => {
          const isCurrent = subscription?.plan === plan.id;
          return (
            <div
              key={plan.id}
              className={`bg-white rounded-lg shadow p-6 ${
                isCurrent ? "ring-2 ring-indigo-500" : ""
              }`}
            >
              <h3 className="text-xl font-bold text-gray-900">{plan.name}</h3>
              <p className="mt-2 text-3xl font-bold text-gray-900">{plan.price}</p>
              <ul className="mt-6 space-y-3">
                {plan.features.map((feature) => (
                  <li key={feature} className="flex items-center text-sm text-gray-600">
                    <svg
                      className="h-4 w-4 text-green-500 mr-2 flex-shrink-0"
                      fill="currentColor"
                      viewBox="0 0 20 20"
                    >
                      <path
                        fillRule="evenodd"
                        d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z"
                        clipRule="evenodd"
                      />
                    </svg>
                    {feature}
                  </li>
                ))}
              </ul>
              <button
                onClick={() => handleSubscribe(plan.id)}
                disabled={isCurrent || actionLoading === plan.id}
                className={`mt-6 w-full py-2 px-4 rounded-md text-sm font-medium ${
                  isCurrent
                    ? "bg-gray-100 text-gray-500 cursor-not-allowed"
                    : "bg-indigo-600 text-white hover:bg-indigo-700"
                } disabled:opacity-50`}
              >
                {isCurrent
                  ? "Current Plan"
                  : actionLoading === plan.id
                  ? "Loading..."
                  : `Subscribe to ${plan.name}`}
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
