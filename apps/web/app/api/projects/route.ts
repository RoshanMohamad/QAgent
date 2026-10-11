import { proxyPost } from "@/lib/proxy";

export async function POST(request: Request) {
  return proxyPost("/api/v1/projects", request, "Could not create the project");
}
