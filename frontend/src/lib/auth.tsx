"use client";

import {
  createContext,
  useContext,
  useEffect,
  useState,
  useCallback,
  ReactNode,
} from "react";
import { useRouter } from "next/navigation";
import { apiGet, apiPost, setTokens, clearTokens, getTokens } from "./api";
import type { User, Organization, AuthTokens } from "./types";

interface AuthState {
  user: User | null;
  org: Organization | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  register: (
    username: string,
    email: string,
    password: string,
    passwordConfirm: string,
    orgName?: string
  ) => Promise<void>;
  logout: () => void;
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [org, setOrg] = useState<Organization | null>(null);
  const [loading, setLoading] = useState(true);
  const router = useRouter();

  const fetchUser = useCallback(async () => {
    try {
      const [userData, orgData] = await Promise.all([
        apiGet<User>("/api/auth/me/"),
        apiGet<Organization>("/api/auth/org/"),
      ]);
      setUser(userData);
      setOrg(orgData);
    } catch {
      setUser(null);
      setOrg(null);
      clearTokens();
    }
  }, []);

  useEffect(() => {
    const tokens = getTokens();
    if (tokens?.access) {
      fetchUser().finally(() => setLoading(false));
    } else {
      setLoading(false);
    }
  }, [fetchUser]);

  const login = async (username: string, password: string) => {
    const data = await apiPost<{ user: User; tokens: AuthTokens }>(
      "/api/auth/login/",
      { username, password }
    );
    setTokens(data.tokens);
    setUser(data.user);
    await fetchUser();
    router.push("/dashboard");
  };

  const register = async (
    username: string,
    email: string,
    password: string,
    passwordConfirm: string,
    orgName?: string
  ) => {
    const data = await apiPost<{ user: User; tokens: AuthTokens }>(
      "/api/auth/register/",
      {
        username,
        email,
        password,
        password_confirm: passwordConfirm,
        org_name: orgName,
      }
    );
    setTokens(data.tokens);
    setUser(data.user);
    await fetchUser();
    router.push("/dashboard");
  };

  const logout = () => {
    clearTokens();
    setUser(null);
    setOrg(null);
    router.push("/login");
  };

  return (
    <AuthContext.Provider
      value={{ user, org, loading, login, register, logout, refreshUser: fetchUser }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used within AuthProvider");
  return context;
}
