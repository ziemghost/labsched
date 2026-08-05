import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "labsched — lab instrument scheduler",
  description: "Capability-authorized scheduling for laboratory instruments",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
