/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The dashboard is a read view over the API; nothing is rendered statically
  // because every number it shows is live.
};

export default nextConfig;
