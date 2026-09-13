import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import { getToken } from "@/lib/session";
import { SignOutButton } from "@/components/sign-out-button";

export const metadata: Metadata = {
  title: "QAgent",
  description: "Autonomous AI software quality engineering platform.",
};

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const signedIn = Boolean(await getToken());

  return (
    <html lang="en">
      <body className="min-h-screen">
        <header
          className="border-b"
          style={{
            background: "var(--surface-1)",
            borderColor: "var(--border)",
          }}
        >
          <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-3">
            <Link href="/" className="flex items-center gap-2">
              <span
                aria-hidden
                className="inline-block h-2.5 w-2.5 rounded-full"
                style={{ background: "var(--accent)" }}
              />
              <span className="text-sm font-semibold">QAgent</span>
            </Link>
            <div className="flex items-center gap-4">
              <p className="text-xs" style={{ color: "var(--text-muted)" }}>
                Quality engineering
              </p>
              {signedIn ? <SignOutButton /> : null}
            </div>
          </div>
        </header>

        <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>

        <footer className="mx-auto max-w-6xl px-6 pb-10">
          <p className="text-xs" style={{ color: "var(--text-muted)" }}>
            Only failures classified as application defects block a deploy.
          </p>
        </footer>
      </body>
    </html>
  );
}
