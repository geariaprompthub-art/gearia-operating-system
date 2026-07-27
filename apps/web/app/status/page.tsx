"use client";

import { useEffect, useState } from "react";

type ApiState = "checking" | "available" | "unavailable";

const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export default function StatusPage() {
  const [state, setState] = useState<ApiState>("checking");

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${apiBaseUrl}/health/ready`, { signal: controller.signal })
      .then((response) => setState(response.ok ? "available" : "unavailable"))
      .catch(() => setState("unavailable"));
    return () => controller.abort();
  }, []);

  const label = state === "checking" ? "Verificando API" : state === "available" ? "API disponível" : "API indisponível";
  return (
    <main className="page-shell status-page">
      <p className="eyebrow">STATUS OPERACIONAL</p>
      <h1>Saúde da plataforma</h1>
      <p className={`status-card ${state}`} role="status">{label}</p>
      <p className="lede">Esta verificação consulta apenas a readiness pública da API. Não exibe dados internos, credenciais ou métricas simuladas.</p>
    </main>
  );
}
