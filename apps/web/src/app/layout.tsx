import type { Metadata } from "next";
import "@xyflow/react/dist/style.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "FlowPilot Studio — Visual Workflow & Bounded AI Agent Editor",
  description:
    "Deterministic orchestration around bounded autonomy. Visual workflow canvas powered by canonical FlowPilot graph schemas.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
