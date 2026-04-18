"use client";

import Link from "next/link";
import { useAuth } from "@/lib/auth";

export default function Navbar() {
  const { user, org, logout } = useAuth();

  if (!user) return null;

  return (
    <nav className="bg-white border-b border-gray-200">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex justify-between h-16">
          <div className="flex items-center gap-8">
            <Link href="/dashboard" className="text-xl font-bold text-indigo-600">
              BehaviorOS
            </Link>
            <div className="hidden md:flex items-center gap-6">
              <Link
                href="/dashboard"
                className="text-gray-600 hover:text-gray-900 text-sm font-medium"
              >
                Dashboard
              </Link>
              <Link
                href="/training"
                className="text-gray-600 hover:text-gray-900 text-sm font-medium"
              >
                Training
              </Link>
              <Link
                href="/settings/business-profile"
                className="text-gray-600 hover:text-gray-900 text-sm font-medium"
              >
                Settings
              </Link>
              <Link
                href="/billing"
                className="text-gray-600 hover:text-gray-900 text-sm font-medium"
              >
                Billing
              </Link>
            </div>
          </div>
          <div className="flex items-center gap-4">
            <span className="text-sm text-gray-500">{org?.name}</span>
            <span className="text-sm font-medium text-gray-700">{user.username}</span>
            <button
              onClick={logout}
              className="text-sm text-gray-500 hover:text-gray-700"
            >
              Logout
            </button>
          </div>
        </div>
      </div>
    </nav>
  );
}
