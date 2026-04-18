"use client";

import { AuthProvider } from "@/lib/auth";
import AppLayout from "@/components/layout/AppLayout";

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <AuthProvider>
      <AppLayout>{children}</AppLayout>
    </AuthProvider>
  );
}
