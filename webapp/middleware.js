import { NextResponse } from "next/server";
import { COOKIE, verifyToken } from "./lib/auth";

export async function middleware(req) {
  const { pathname } = req.nextUrl;
  if (pathname.startsWith("/login") || pathname.startsWith("/api/auth")) {
    return NextResponse.next();
  }
  const user = await verifyToken(req.cookies.get(COOKIE)?.value);
  if (!user) {
    if (pathname.startsWith("/api/")) {   // fetch() callers want JSON, not HTML
      return NextResponse.json({ error: "unauthorized" }, { status: 401 });
    }
    const url = req.nextUrl.clone();
    url.pathname = "/login";
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

// protect everything except static assets and files with extensions
export const config = {
  matcher: ["/((?!_next|favicon.ico|.*\\..*).*)"],
};
