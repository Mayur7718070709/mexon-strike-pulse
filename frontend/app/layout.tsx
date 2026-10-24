import type { Metadata, Viewport } from "next";
import "./styles.css";
import "./analysis.css";
import "./auth.css";
import "./historical-admin.css";
import "./workspace.css";

export const metadata: Metadata = {
  title: "Market Pulse | Mexon Intelligence",
  description: "Live market and options intelligence powered by DhanHQ.",
  manifest: "/manifest.webmanifest",
};
export const viewport: Viewport = { themeColor: "#090d14", width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
