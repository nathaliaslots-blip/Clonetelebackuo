# Lives encaminhadas

Userbot em Python com Telethon que lê mídias do grupo de origem, extrai
nome/ID/país da legenda, encontra ou cria um tópico no grupo de destino e
encaminha a mídia sem autoria nem legenda original. O encaminhamento para
tópicos usa a requisição raw da API do Telegram, pois o helper de alto nível
do Telethon não expõe esse parâmetro em algumas versões.

## Configuração local

1. Crie o `.env` a partir do `.env.example`.
2. Obtenha `API_ID` e `API_HASH` em https://my.telegram.org.
3. Gere a `STRING_SESSION` uma vez:

   ```bash
   pip install -r requirements.txt
   python -c "from telethon.sync import TelegramClient; from telethon.sessions import StringSession; import os; print(TelegramClient(StringSession(), int(os.environ['API_ID']), os.environ['API_HASH']).start().session.save())"
   ```

   Execute com `API_ID` e `API_HASH` exportados no ambiente. O login pede seu
   telefone, código do Telegram e, se houver, a senha 2FA. Nunca publique a
   string gerada.

4. Instale e execute:

   ```bash
   pip install -r requirements.txt
   python main.py
   ```

Cada mídia é encaminhada duas vezes: primeiro para o tópico correspondente ao
`user_id` e também para o tópico fixo `EXTRA_TOPIC_ID` (padrão `264`).
O encaminhamento procura primeiro o tópico correspondente ao `user_id` no
`topics.json`. Se não encontrar, consulta os tópicos do grupo e, por fim, cria
um tópico com o título `{bandeira} {nome} - {id}`. O tópico `264` pode ser
alterado com `EXTRA_TOPIC_ID`; ele não é criado pelo bot.
O arquivo `topics.json` é criado automaticamente e não deve ser versionado.

## Mensagens agendadas

Quando `OTHER_GROUP` estiver configurada, o bot consulta até 100 mensagens
agendadas desse grupo a cada 30 segundos. Para cada legenda reconhecida pelo
mesmo parser usado nas mídias recebidas, ele extrai nome/ID/país e edita a
mensagem com a legenda padronizada. Há um intervalo de 5 segundos entre
edições. Mensagens sem legenda compatível são ignoradas. O bot também garante
a criação do tópico correspondente no grupo de destino, sem encaminhar a mídia.

## Railway

Crie um serviço a partir deste repositório, configure os secrets
`API_ID`, `API_HASH`, `STRING_SESSION`, `SOURCE_CHAT_ID` e `LOG_CHAT_ID` no
Railway e faça o deploy. Para ativar a edição de mensagens agendadas, configure
também `OTHER_GROUP`. `TARGET_CHAT_ID`, `EXTRA_TOPIC_ID` e `QUEUE_DELAY_SECONDS`
são opcionais.
O processo de execução é `python main.py`. A `STRING_SESSION` é obrigatória no Railway:
sem ela, o Telethon tenta pedir o telefone via terminal e ocorre
`EOFError` porque o ambiente não é interativo.
Use uma conta autorizada pelo grupo de origem e mantenha a sessão privada.

As mídias entram em uma fila e são processadas por um único worker, com dois
segundos entre itens. O bot envia apenas sucessos, descartes, erros e
`FloodWait` para `LOG_CHAT_ID`.

No grupo de logs, o dono da conta pode usar `/topico` para consultar a
quantidade total de tópicos, `/sync` para reconstruir o cache completo,
`/duplicados` para listar tópicos duplicados, ou `/clone` seguido de IDs de
mensagens do grupo de origem. A clonagem também passa pela fila e encaminha
para o tópico da pessoa e para o tópico Geral (`id=1`).