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
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" style={{ width: 440 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title">
            {mode === "login" ? <LogIn size={16} /> : <UserPlus size={16} />}
            <span>{mode === "login" ? "Sign In to FlowPilot" : "Create Organization"}</span>
          </div>
          <button
            type="button"
            className="btn btn-ghost btn-icon"
            onClick={onClose}
            aria-label="Close"
          >
            <X size={15} />
          </button>
        </div>

        <div
          style={{
            display: "flex",
            gap: 4,
            marginTop: 14,
            marginBottom: 16,
            padding: 3,
            borderRadius: 8,
            background: "var(--bg-elevated)",
            border: "1px solid var(--border)",
          }}
        >
          <button
            type="button"
            className={`btn ${mode === "register" ? "btn-primary" : "btn-ghost"}`}
            style={{ flex: 1, height: 28 }}
            onClick={() => setMode("register")}
          >
            Register
          </button>
          <button
            type="button"
            className={`btn ${mode === "login" ? "btn-primary" : "btn-ghost"}`}
            style={{ flex: 1, height: 28 }}
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

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 16 }}>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
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
