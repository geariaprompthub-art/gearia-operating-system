export default function Home() {
  return (
    <main className="page-shell">
      <section className="hero">
        <p className="eyebrow">CONSOLE OPERACIONAL</p>
        <h1>GearIA Operating System</h1>
        <p className="lede">
          Base operacional para ingestão, enriquecimento e recuperação de conteúdo.
          Os módulos abaixo refletem apenas capacidades já disponíveis na plataforma.
        </p>
        <a className="primary-action" href="/status">Verificar status da plataforma</a>
      </section>
      <section className="module-grid" aria-label="Módulos disponíveis">
        <article><h2>Fontes e Scout</h2><p>Cadastro de fontes e ingestão RSS com proteção de rede.</p></article>
        <article><h2>Conteúdos</h2><p>Normalização, deduplicação e enriquecimento determinístico.</p></article>
        <article><h2>Recuperação</h2><p>Busca lexical, vetorial, híbrida e reranking já disponíveis na API.</p></article>
      </section>
    </main>
  );
}
