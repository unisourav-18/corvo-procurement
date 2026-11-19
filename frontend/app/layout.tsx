import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Corvo Engineering — Policy-Driven PR-to-PO Portal",
  description: "Autonomous policy-driven procurement intake, decision engine, and governance workspace",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-slate-50 text-slate-900 antialiased">{children}</body>
    </html>
  );
}