"use client";

import { useState } from "react";
import { LogIn, UserPlus, X } from "lucide-react";

interface AuthModalProps {
  open: boolean;
  loading: boolean;
  onClose: () => void;
  onLogin: (email: string, password: string) => Promise<void>;
  onRegister: (
    email: string,
    password: string,
    orgName: string,
    orgSlug: string,
  ) => Promise<void>;
}

export function AuthModal({ open, loading, onClose, onLogin, onRegister }: AuthModalProps) {
  const [mode, setMode] = useState<"login" | "register">("register");
  const [email, setEmail] = useState("founder@acme.io");
  const [password, setPassword] = useState("flowpilot-pass-123");
  const [orgName, setOrgName] = useState("Acme Automation");
  const [orgSlug, setOrgSlug] = useState("acme-automation");

  if (!open) return null;

  return (
    <div className="modal-backdrop">
      <div className="modal-card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 700, fontSize: 15 }}>
            {mode === "login" ? <LogIn size={16} /> : <UserPlus size={16} />}
            <span>{mode === "login" ? "Sign In to FlowPilot" : "Create FlowPilot Organization"}</span>
          </div>
          <button className="btn" style={{ height: 28 }} onClick={onClose}>
            <X size={14} />
          </button>
        </div>

        <div style={{ display: "flex", gap: 8, marginTop: 12, marginBottom: 14 }}>
          <button
            className={`btn ${mode === "register" ? "btn-primary" : ""}`}
            style={{ flex: 1, justifyContent: "center" }}
            onClick={() => setMode("register")}
          >
            Register
          </button>
          <button
            className={`btn ${mode === "login" ? "btn-primary" : ""}`}
            style={{ flex: 1, justifyContent: "center" }}
            onClick={() => setMode("login")}
          >
            Sign In
          </button>
        </div>

        <div className="field-group">
          <label className="field-label">Email</label>
          <input className="input" value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        <div className="field-group">
          <label className="field-label">Password</label>
          <input
            className="input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>

        {mode === "register" && (
          <>
            <div className="field-group">
              <label className="field-label">Organization Name</label>
              <input
                className="input"
                value={orgName}
                onChange={(e) => setOrgName(e.target.value)}
              />
            </div>
            <div className="field-group">
              <label className="field-label">Organization Slug</label>
              <input
                className="input"
                value={orgSlug}
                onChange={(e) => setOrgSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))}
              />
            </div>
          </>
        )}

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 14 }}>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn-primary"
            disabled={loading}
            onClick={() =>
              mode === "login"
                ? onLogin(email, password)
                : onRegister(email, password, orgName, orgSlug)
            }
          >
            {loading ? "Authenticating..." : mode === "login" ? "Sign In" : "Create Workspace"}
          </button>
        </div>
      </div>
    </div>
  );
}
