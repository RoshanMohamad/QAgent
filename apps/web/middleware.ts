import { NextRequest, NextResponse } from "next/server";

// Must match SESSION_COOKIE in lib/session.ts. Duplicated because next/headers'
// cookies() isn't usable in the edge middleware runtime.
const SESSION_COOKIE = "qagent_token";

export function middleware(request: NextRequest) {
  const token = request.cookies.get(SESSION_COOKIE)?.value;
  if (!token) {
    const loginUrl = new URL("/login", request.url);
    return NextResponse.redirect(loginUrl);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!login|api|_next/static|_next/image|favicon.ico).*)"],
};
