# P2B — Fase 7: anonimização e encerramento de conta

`GET /auth/me` continua sendo o perfil mínimo somente-leitura. `PATCH /auth/me`
foi adiado: não há campo pessoal mutável aprovado no modelo `User`, e criar um
campo apenas para suportar a rota ampliaria o escopo.

`DELETE /auth/me` requer principal autenticado, cookie e header CSRF vinculados
à sessão e o corpo estrito `{ "confirmation": "DELETE" }`. A resposta é `204`
sem corpo e usa `Cache-Control: no-store`; os cookies de access, refresh e CSRF
são removidos pela política centralizada.

Em uma transação, o serviço bloqueia `User`, bloqueia seu `Workspace`, revoga
sessões, remove refresh-token hashes e challenges de lifecycle, e substitui o
e-mail por `deleted+<user_uuid>@invalid.local`. Também remove o hash de senha,
limpa metadados pessoais, incrementa `token_version`, muda o usuário para
`anonymized` e bloqueia o workspace com
`blocked_by_owner_anonymization`. A operação é irreversível na P2B: não remove
dados canônicos, fontes, conteúdos ou projeções de visibilidade.

Workspaces bloqueados falham fechados na resolução de `WorkspaceContext` e nas
operações de serviço. O e-mail anterior fica disponível para um novo cadastro,
sem associação ao workspace ou às credenciais da conta retida.
