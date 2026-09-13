import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { SESSION_COOKIE } from "@/lib/session";

const BASE_URL = process.env.QAGENT_API_URL ?? "http://127.0.0.1:8000";
const TOKEN_TTL_SECONDS = 12 * 60 * 60; // must match ACCESS_TOKEN_TTL in modules/auth/security.py

export async function POST(request: Request) {
  const body = await request.json();

  let response: Response;
  try {
    response = await fetch(`${BASE_URL}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    return NextResponse.json(
      { error: `Cannot reach the QAgent API at ${BASE_URL}` },
      { status: 503 },
    );
  }

  const payload = await response.json();
  if (!response.ok) {
    return NextResponse.json(
      { error: payload.detail ?? "Sign in failed" },
      { status: response.status },
    );
  }

  const store = await cookies();
  store.set(SESSION_COOKIE, payload.access_token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: TOKEN_TTL_SECONDS,
  });

  return NextResponse.json({ ok: true });
}
