import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "GearIA Operating System",
  description: "AI-powered Operating System for GearIA",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="pt-BR"><body>{children}</body></html>;
}
