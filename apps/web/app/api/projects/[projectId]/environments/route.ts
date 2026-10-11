import { proxyPost } from "@/lib/proxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ projectId: string }> },
) {
  const { projectId } = await params;
  return proxyPost(
    `/api/v1/projects/${encodeURIComponent(projectId)}/environments`,
    request,
    "Could not add the environment",
  );
}
