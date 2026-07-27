import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "GearIA Operating System",
  description: "AI-powered Operating System for GearIA",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="pt-BR">
      <body>
        <header className="site-header">
          <a className="brand" href="/">GearIA <span>OS</span></a>
          <nav aria-label="Navegação principal">
            <a href="/">Visão geral</a>
            <a href="/status">Status</a>
            <a href="http://localhost:8000/docs" target="_blank" rel="noreferrer">API docs</a>
          </nav>
        </header>
        {children}
      </body>
    </html>
  );
}
