import { proxyDownload } from "@/lib/proxy";

/**
 * Downloads one piece of defect evidence (screenshot, log, HAR, trace).
 *
 * The API filters artifacts by organization, so an id from another tenant is
 * a 404 here exactly as it is upstream.
 */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ artifactId: string }> },
) {
  const { artifactId } = await params;
  return proxyDownload(`/api/v1/artifacts/${encodeURIComponent(artifactId)}`);
}
