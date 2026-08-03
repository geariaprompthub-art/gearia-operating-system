# P2B — Fase 4: cadastro público

`POST /auth/register` aceita exclusivamente `email` e `password` e sempre devolve
`202 {"status":"registration_received"}` para cadastro novo, reemissão de uma
conta pendente e estados existentes não elegíveis. O contrato não expõe usuário,
workspace, token, hash, estado interno ou verificação de e-mail.

O request é estrito: campos adicionais e entradas inválidas retornam `422`.
Os limites Redis usam os namespaces `auth:register:ip` e `auth:register:email`;
o segundo é derivado pelo limitador, sem chave Redis contendo o e-mail em claro.
Uma recusa retorna `429`, inclui `Retry-After` e não inicia o cadastro.

O `RegistrationService` continua como único owner de commit/rollback. Depois de
seu retorno bem-sucedido, a camada de aplicação chama `EmailDeliveryAdapter`.
Nesta fase o único adaptador é `FakeEmailDeliveryAdapter`, sem rede e sem captura
de token em produção. Falha de entrega acontece depois do commit, não desfaz o
usuário, workspace ou token, e recebe somente registro estruturado sanitizado.

As respostas de sucesso e de limitação usam `Cache-Control: no-store`. Cadastro
é anônimo e não cria sessão/cookie; portanto CSRF não se aplica nesta rota nesta
fase. Os fluxos autenticados que alteram estado continuam protegidos pela política
CSRF centralizada.
